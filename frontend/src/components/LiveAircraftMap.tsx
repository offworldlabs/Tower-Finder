// @ts-nocheck — gradual TS migration; will type incrementally
import React, { useEffect, useRef, useState, useCallback, useMemo, memo } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  CircleMarker,
  Circle,
  Polygon,
  Polyline,
  useMap,
  useMapEvents,
} from "react-leaflet";
import L from "leaflet";
import "./LiveAircraftMap.css";

import {
  STALE_AIRCRAFT_MS,
  GT_FEED_STALE_MS,
  GT_PRUNE_GRACE_MS,
  POSITION_SOURCE_ARC_ONLY,
  ARC_DR_MAX_S,
  groundTruthKey,
  applyGroundTruthFixes,
  pruneGroundTruthFixes,
  sweepStaleGroundTruthFixes,
  isPointInViewport,
  isAircraftInViewport,
  sampleTrailPositions,
  buildTrailSegments,
  makeAircraftIcon,
  makeDroneIcon,
  nodeIcon,
  yagiSectorPositions,
  FitBounds,
  ViewportTracker,
  MapClickClear,
  useAircraftFeed,
  useNodes,
  useAuth,
  NodeOwnerControl,
  AircraftListPanel,
  AircraftDetailPanel,
  Toolbar,
  PlaybackBar,
  DetectionArcs,
  InBeamDiagnostic,
} from "./map";

import { fetchNodeVerification, fetchMlatVerification, fetchMlatHistory } from "../api";
import { defaultsGroundTruthOff } from "../utils/domains";
import { usePersistedState } from "./map/usePersistedState";
import { parseHash, useHashWriter, encodeLayers, decodeLayers } from "./map/useUrlHashState";
import { useKeyboardShortcuts } from "./map/useKeyboardShortcuts";
import { trailToCsv, trailsToBulkCsv, downloadCsv } from "./map/trailExport";
import { toast, copyToClipboard } from "./map/toast";
import { checkEmergencySquawks, resetEmergencyAlertCache } from "./map/emergencyAudio";
import { distanceKm } from "./map/distance";
import { validLatLon } from "./map/geo";
import { arcNearestPoint } from "./map/arcErrors";
import { detectingNodeIdsFor } from "./map/detections";
import { ensureDebugPanes, DEBUG_PASSIVE_PANE, GT_CLICK_PANE } from "./map/panes";
import { ARC_TOTAL_LIFE_MS } from "./map/constants";
import { snapTrack, sweepStaleRadar } from "./map/trackStores";
import StatsOverlay from "./map/StatsOverlay";
import ShortcutHelp from "./map/ShortcutHelp";

// Fix default icon paths
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl:
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

/* ── GroundTruthCanvasLayer: renders all truth-only dots on a single <canvas> element.
      With 500+ objects, React-managed SVG CircleMarkers cause severe lag on every
      WS update (~1Hz). L.canvas() draws everything in one canvas tile — O(1) DOM.
      This is the ONE canvas that keeps pointer events (see map/panes.ts) — its
      dots carry the only canvas click handlers on the map. ── */
const _gtCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5, pane: GT_CLICK_PANE }) : null;

const GroundTruthCanvasLayer = memo(function GroundTruthCanvasLayer({ aircraft, onSelect, selectedHex }) {
  const map = useMap();
  const markerMapRef = useRef(new Map()); // hex → L.circleMarker — incremental diff
  const onSelectRef  = useRef(onSelect);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);

  useEffect(() => {
    ensureDebugPanes(map);
    const markerMap = markerMapRef.current;
    const seen = new Set();

    for (const ac of aircraft) {
      seen.add(ac.hex);
      const isAnom  = ac.is_anomalous;
      const isDrone = ac.object_type === "drone";
      const isSel   = ac.hex === selectedHex;
      const color   = isAnom ? "#f43f5e" : isDrone ? "#f59e0b" : "#22d3ee";
      // Selection ring is white so it reads against all three fill colors.
      const border  = isSel ? "#f8fafc" : isAnom ? "#e11d48" : isDrone ? "#d97706" : "#67e8f9";
      const baseR   = isDrone ? 6 : isAnom ? 8 : 9;
      const radius  = isSel ? baseR + 4 : baseR;
      const weight  = isSel ? 4 : 3;

      let m = markerMap.get(ac.hex);
      if (!m) {
        m = L.circleMarker([ac.lat, ac.lon], {
          renderer: _gtCanvas,
          // Without this, the click also fires the map's click handler
          // (MapClickClear), which deselects in the same React batch — the
          // dot appeared dead even when the hit test found it.
          bubblingMouseEvents: false,
          radius,
          color: border,
          weight,
          fillColor: color,
          fillOpacity: 0.7,
        });
        m.on("click", () => onSelectRef.current(ac.hex));
        m.addTo(map);
        markerMap.set(ac.hex, m);
      } else {
        m.setLatLng([ac.lat, ac.lon]);
        m.setStyle({ color: border, fillColor: color, weight });
        if (m.options.radius !== radius) m.setRadius(radius);
      }
    }

    // Remove markers for aircraft that left the list
    for (const [hex, m] of markerMap) {
      if (!seen.has(hex)) {
        m.remove();
        markerMap.delete(hex);
      }
    }
  }, [aircraft, map, selectedHex]);

  // Full cleanup on unmount
  useEffect(() => {
    return () => {
      for (const m of markerMapRef.current.values()) m.remove();
      markerMapRef.current.clear();
    };
  }, [map]);

  return null;
});

/* ── MatchedGroundTruthLayer: shows GT positions for radar-matched aircraft + error line.
      Renders as imperative L.circleMarker (GT dot) + L.polyline (error vector) on a
      single canvas.  Updated at 4Hz from smoothRef for dead-reckoned positions.
      Purely visual — everything renders non-interactive in the passive pane so
      it can't swallow clicks aimed at the GT dots underneath; the km label is a
      permanent tooltip since there is no hover target anymore. ── */
const _mgCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5, pane: DEBUG_PASSIVE_PANE }) : null;

// Ref-driven like DetectionArcs: data is read INSIDE the tick, so the effect
// mounts once instead of keying on the 2 Hz radarAircraft array identity —
// which tore down and recreated every dot and line twice a second.
const MatchedGroundTruthLayer = memo(function MatchedGroundTruthLayer({ radarAircraftRef, groundTruthRef, smoothRef, nodesByIdRef }) {
  const map = useMap();
  const markersRef = useRef(new Map());  // gtHex → { dot: L.circleMarker, line: L.polyline }

  useEffect(() => {
    ensureDebugPanes(map);
    const markers = markersRef.current;

    const tick = () => {
      const gt = groundTruthRef.current;
      const seen = new Set();

      for (const ac of radarAircraftRef.current || []) {
        const gtHex = ac.ground_truth_hex;
        if (!gtHex) continue;
        const gtTrail = gt[gtHex];
        if (!Array.isArray(gtTrail) || gtTrail.length === 0) continue;

        // GT position from smoothRef (dead-reckoned at 60fps) if available, else raw
        const smooth = smoothRef.current[groundTruthKey(gtHex)];
        let gtLat, gtLon;
        if (smooth) {
          gtLat = smooth.lat;
          gtLon = smooth.lon;
        } else {
          const last = gtTrail[gtTrail.length - 1];
          gtLat = last[0];
          gtLon = last[1];
        }

        // Radar position from smoothRef
        const radarSmooth = smoothRef.current[ac.hex];
        let rLat = radarSmooth ? radarSmooth.lat : ac.lat;
        let rLon = radarSmooth ? radarSmooth.lon : ac.lon;

        if (!validLatLon(gtLat, gtLon) || !validLatLon(rLat, rLon)) continue;

        // Arc-only tracks: their lat/lon is the arc MIDPOINT — a convention,
        // not an estimate — so the honest error vector runs from GT to the
        // nearest point of the measured locus, not to the midpoint.
        let errKm;
        if (ac.position_source === POSITION_SOURCE_ARC_ONLY) {
          const near = arcNearestPoint(
            ac, nodesByIdRef?.current?.[ac.node_id], gtLat, gtLon,
          );
          if (near) {
            rLat = near.lat;
            rLon = near.lon;
            errKm = near.distKm;
          }
        }
        if (errKm == null) errKm = distanceKm(gtLat, gtLon, rLat, rLon);
        const label = `${errKm.toFixed(1)} km`;

        seen.add(gtHex);
        let entry = markers.get(gtHex);
        if (!entry) {
          const dot = L.circleMarker([gtLat, gtLon], {
            renderer: _mgCanvas,
            interactive: false,
            radius: 5,
            color: "#22d3ee",
            weight: 2,
            fillColor: "#22d3ee",
            fillOpacity: 0.8,
          });
          const line = L.polyline([[gtLat, gtLon], [rLat, rLon]], {
            renderer: _mgCanvas,
            interactive: false,
            color: "#facc15",
            weight: 1.5,
            opacity: 0.6,
            dashArray: "3 4",
          });
          line.bindTooltip(label, { permanent: true, direction: "center", className: "radar3-error-label" });
          dot.addTo(map);
          line.addTo(map);
          entry = { dot, line };
          markers.set(gtHex, entry);
        } else {
          entry.dot.setLatLng([gtLat, gtLon]);
          entry.line.setLatLngs([[gtLat, gtLon], [rLat, rLon]]);
          entry.line.setTooltipContent(label);
          // A permanent tooltip is positioned once at open — walk it along
          // with the line's midpoint or it stays where the line first drew.
          entry.line.getTooltip()?.setLatLng([(gtLat + rLat) / 2, (gtLon + rLon) / 2]);
        }
      }

      // Remove markers for aircraft no longer matched
      for (const [hex, entry] of markers) {
        if (!seen.has(hex)) {
          entry.dot.remove();
          entry.line.remove();
          markers.delete(hex);
        }
      }
    };

    tick();
    const intervalId = setInterval(tick, 250);
    return () => {
      clearInterval(intervalId);
      for (const entry of markers.values()) {
        entry.dot.remove();
        entry.line.remove();
      }
      markers.clear();
    };
  }, [map, radarAircraftRef, groundTruthRef, smoothRef]);

  return null;
});

/* ── NodeVerificationLayer: shows solver tracks vs ADS-B truth for one node.
      Fetches verification data every 15s and renders cyan truth dots,
      yellow error lines, and distance labels.  Pass nodeId=null to hide. ── */
const _verifyCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5, pane: DEBUG_PASSIVE_PANE }) : null;

const NodeVerificationLayer = memo(function NodeVerificationLayer({ nodeId }) {
  const map = useMap();
  const markersRef = useRef(new Map());

  useEffect(() => {
    ensureDebugPanes(map);
    if (!nodeId) {
      // Clean up when hidden
      for (const entry of markersRef.current.values()) {
        entry.dot.remove();
        entry.line.remove();
        }
      markersRef.current.clear();
      return;
    }

    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await fetchNodeVerification(nodeId);
        if (cancelled || !data) return;

        const markers = markersRef.current;
        const seen = new Set();

        for (const t of data.tracks || []) {
          if (!validLatLon(t.truth_lat, t.truth_lon) || !validLatLon(t.solver_lat, t.solver_lon)) continue;
          seen.add(t.hex);

          let entry = markers.get(t.hex);
          if (!entry) {
            const dot = L.circleMarker([t.truth_lat, t.truth_lon], {
              renderer: _verifyCanvas,
              interactive: false,
              radius: 5,
              color: "#22d3ee",
              weight: 2,
              fillColor: "#22d3ee",
              fillOpacity: 0.8,
            });
            const line = L.polyline(
              [[t.truth_lat, t.truth_lon], [t.solver_lat, t.solver_lon]],
              { renderer: _verifyCanvas, interactive: false, color: "#facc15", weight: 1.5, opacity: 0.7, dashArray: "3 4" },
            );
            line.bindTooltip(`${t.position_error_km.toFixed(1)} km`, { permanent: true, direction: "center", className: "radar3-error-label" });
            dot.addTo(map);
            line.addTo(map);
            entry = { dot, line };
            markers.set(t.hex, entry);
          } else {
            entry.dot.setLatLng([t.truth_lat, t.truth_lon]);
            entry.line.setLatLngs([[t.truth_lat, t.truth_lon], [t.solver_lat, t.solver_lon]]);
            entry.line.setTooltipContent(`${t.position_error_km.toFixed(1)} km`);
            entry.line.getTooltip()?.setLatLng([
              (t.truth_lat + t.solver_lat) / 2, (t.truth_lon + t.solver_lon) / 2,
            ]);
          }
        }

        for (const [hex, entry] of markers) {
          if (!seen.has(hex)) {
            entry.dot.remove();
            entry.line.remove();
            markers.delete(hex);
          }
        }
      } catch {
        // Silently ignore fetch errors
      }
    };

    refresh();
    const intervalId = setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
      for (const entry of markersRef.current.values()) {
        entry.dot.remove();
        entry.line.remove();
      }
      markersRef.current.clear();
    };
  }, [map, nodeId]);

  return null;
});

/* ── MlatVerificationLayer: shows multinode (MLAT) solver positions vs ground-truth.

      The verification payload reports (truth, solver) coordinates frozen at
      the solver's capture timestamp — anywhere from 5 to 60 s old by the
      time we render. Drawing those raw lat/lons would leave the magenta
      dot trailing behind the live cyan ADS-B circle by minutes-of-arc
      worth of aircraft motion (≈ 2-10 km for typical jets), even though
      the underlying error magnitude is tiny.

      Fix: translate the (truth, solver) pair forward to "now" by anchoring
      the truth point to the live ADS-B position from `smoothRef` and
      shifting the solver point by the same vector. The error vector itself
      is preserved — only its frame of reference moves — so the magenta dot
      sits on top of the cyan circle and the dashed line shows the actual
      solver-vs-truth offset at the current aircraft location. ── */
const _mlatCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5, pane: DEBUG_PASSIVE_PANE }) : null;

const MlatVerificationLayer = memo(function MlatVerificationLayer({ groundTruthRef, smoothRef }) {
  const map = useMap();
  const markersRef = useRef(new Map());
  // Tracks fetched from the verification API. Mutable so the render tick
  // can read it without re-running the polling effect.
  const tracksRef = useRef([]);

  // Poll the verification API on its own cadence (the data itself only
  // refreshes server-side every ~30 s).
  useEffect(() => {
    const ACTIVE_POLL_MS = 15000;
    const IDLE_POLL_MS = 60000;
    let cancelled = false;
    let timerId = null;

    const scheduleNext = (delayMs) => {
      if (cancelled) return;
      timerId = window.setTimeout(() => {
        refresh();
      }, delayMs);
    };

    const refresh = async () => {
      let nextDelayMs = ACTIVE_POLL_MS;
      try {
        const data = await fetchMlatVerification();
        if (cancelled) return;
        if (!data) {
          nextDelayMs = IDLE_POLL_MS;
          return;
        }
        tracksRef.current = data.tracks || [];
        nextDelayMs = (data.n_matched || 0) > 0 ? ACTIVE_POLL_MS : IDLE_POLL_MS;
      } catch {
        nextDelayMs = IDLE_POLL_MS;
      } finally {
        scheduleNext(nextDelayMs);
      }
    };

    refresh();
    return () => {
      cancelled = true;
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    };
  }, []);

  // Re-anchor marker positions to live truth on a fast tick so the magenta
  // dot stays glued to the cyan circle as the aircraft moves between API
  // refreshes.
  useEffect(() => {
    ensureDebugPanes(map);
    const markers = markersRef.current;

    const tick = () => {
      const seen = new Set();
      const tracks = tracksRef.current;

      for (const t of tracks) {
        if (!validLatLon(t.truth_lat, t.truth_lon) || !validLatLon(t.solver_lat, t.solver_lon)) continue;
        const hex = t.truth_hex;
        if (!hex) continue;

        // Live truth position: prefer the smoothed value, fall back to the
        // most recent raw trail point. Skip if we have neither — the
        // verification payload alone isn't enough to render aligned.
        let liveLat;
        let liveLon;
        const smooth = smoothRef?.current?.[groundTruthKey(hex)];
        if (smooth) {
          liveLat = smooth.lat;
          liveLon = smooth.lon;
        } else {
          const trail = groundTruthRef?.current?.[hex];
          if (Array.isArray(trail) && trail.length) {
            const last = trail[trail.length - 1];
            liveLat = last[0];
            liveLon = last[1];
          }
        }
        if (liveLat == null || liveLon == null) continue;

        // Translate the (truth, solver) pair forward by (now - solve_ts).
        // The dr_truth coincides with where ADS-B says the aircraft is now;
        // dr_solver is offset by the original solve-time error vector, so
        // the dashed line still represents the real position error.
        const drTruthLat = liveLat;
        const drTruthLon = liveLon;
        const drSolverLat = liveLat + (t.solver_lat - t.truth_lat);
        const drSolverLon = liveLon + (t.solver_lon - t.truth_lon);

        const id = t.solve_key;
        seen.add(id);

        let entry = markers.get(id);
        if (!entry) {
          const dot = L.circleMarker([drTruthLat, drTruthLon], {
            renderer: _mlatCanvas,
            interactive: false,
            radius: 4,
            color: "#e879f9",
            weight: 2,
            fillColor: "#e879f9",
            fillOpacity: 0.85,
          });
          const line = L.polyline(
            [[drTruthLat, drTruthLon], [drSolverLat, drSolverLon]],
            { renderer: _mlatCanvas, interactive: false, color: "#f0abfc", weight: 1.5, opacity: 0.7, dashArray: "3 4" },
          );
          line.bindTooltip(
            `${t.position_error_km.toFixed(1)} km`,
            { permanent: true, direction: "center", className: "radar3-error-label" },
          );
          dot.addTo(map);
          line.addTo(map);
          entry = { dot, line };
          markers.set(id, entry);
        } else {
          entry.dot.setLatLng([drTruthLat, drTruthLon]);
          entry.line.setLatLngs([[drTruthLat, drTruthLon], [drSolverLat, drSolverLon]]);
          entry.line.setTooltipContent(`${t.position_error_km.toFixed(1)} km`);
          entry.line.getTooltip()?.setLatLng([
            (drTruthLat + drSolverLat) / 2, (drTruthLon + drSolverLon) / 2,
          ]);
        }
      }

      // Remove markers whose solve_key is no longer in the payload.
      for (const [id, entry] of markers) {
        if (!seen.has(id)) {
          entry.dot.remove();
          entry.line.remove();
          markers.delete(id);
        }
      }
    };

    tick();
    const intervalId = setInterval(tick, 250);
    return () => {
      clearInterval(intervalId);
      for (const entry of markers.values()) {
        entry.dot.remove();
        entry.line.remove();
      }
      markers.clear();
    };
  }, [map, groundTruthRef, smoothRef]);

  return null;
});

/* ── MlatSolveHistoryLayer: raw per-solve positions behind the selected MLAT
      marker (from /api/test/mlat-history), so "solve trail vs GT trail vs
      displayed marker" is visually decomposable.  Dots only, no interaction —
      the detail panel's solve-history table is the lookup surface.  Bounded
      (≤60 dots for one selected track), so React CircleMarkers are fine. ── */
const MlatSolveHistoryLayer = memo(function MlatSolveHistoryLayer({ solves }) {
  const errColor = (e) =>
    e == null ? "#94a3b8" : e < 3 ? "#34d399" : e < 8 ? "#f59e0b" : "#f43f5e";
  return (
    <>
      {solves.slice(0, 60).map((s, i) =>
        validLatLon(s.raw_lat, s.raw_lon) ? (
          <CircleMarker
            key={`${s.ts_ms}-${i}`}
            center={[s.raw_lat, s.raw_lon]}
            radius={3}
            interactive={false}
            pathOptions={{
              color: errColor(s.gt_error_km),
              weight: 1,
              opacity: 0.9,
              fillColor: errColor(s.gt_error_km),
              // Newest first in the payload — older solves fade out.
              fillOpacity: Math.max(0.15, 0.75 - i * 0.02),
            }}
          />
        ) : null,
      )}
    </>
  );
});

/* ── AircraftMarker: memoized with custom comparator — only re-renders on visual changes
      (selection, labels, callsign, altitude band, type).  lat/lon/track/gs are updated
      imperatively at 60fps via markerRegistry → marker.setLatLng() in the RAF loop,
      completely bypassing React reconcile. ── */
const AircraftMarker = memo(function AircraftMarker({ ac, isSelected, showLabels, colorByAlt, onSelect, markerRegistry }) {
  const altBand = Math.floor((ac.alt_baro ?? 0) / 5000);
  const markerRef = useRef(null);

  // Register/unregister in the parent's imperative registry so the RAF loop can
  // call marker.setLatLng() at 60fps without going through React state.
  useEffect(() => {
    const m = markerRef.current;
    if (m) markerRegistry.set(ac.hex, m);
    return () => { markerRegistry.delete(ac.hex); };
  }, [ac.hex, markerRegistry]);

  const icon = useMemo(
    () => ac.target_class === "drone"
      ? makeDroneIcon(ac, showLabels, isSelected)
      : makeAircraftIcon(ac, showLabels, isSelected, colorByAlt),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ac.hex, isSelected, showLabels, colorByAlt, ac.flight, ac.target_class, altBand],
  );
  const handlers = useMemo(() => ({ click: () => onSelect(ac.hex) }), [ac.hex, onSelect]);
  return <Marker ref={markerRef} position={[ac.lat, ac.lon]} icon={icon} eventHandlers={handlers} />;
}, (prev, next) =>
  // Skip re-render when ONLY position/velocity changed — those are patched live
  // by the RAF loop via marker.setLatLng() without touching React at all.
  prev.isSelected === next.isSelected &&
  prev.showLabels === next.showLabels &&
  prev.colorByAlt === next.colorByAlt &&
  prev.ac.hex === next.ac.hex &&
  prev.ac.flight === next.ac.flight &&
  prev.ac.target_class === next.ac.target_class &&
  Math.floor((prev.ac.alt_baro ?? 0) / 5000) === Math.floor((next.ac.alt_baro ?? 0) / 5000) &&
  prev.onSelect === next.onSelect
);

/* ── AircraftTrailsLayer: imperative L.polyline per visible aircraft, fed by
      frontendTrailsRef (per-hex buffer of smoothed positions sampled at 2 Hz).
      Updated at 2 Hz so 100+ trails stay cheap; uses a single L.canvas
      renderer so all trails draw on one canvas tile instead of N <path>
      elements.  Skips the selected aircraft (its prominent trail is rendered
      separately by the existing selectedTrailPositions block). ── */
const _trailsCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5, pane: DEBUG_PASSIVE_PANE }) : null;

// Ref-driven (see MatchedGroundTruthLayer): keying the effect on the 2 Hz
// visibleAircraft array identity destroyed and rebuilt every polyline twice a
// second, and the 500 ms interval below essentially never fired twice.
const AircraftTrailsLayer = memo(function AircraftTrailsLayer({ visibleAircraftRef, frontendTrailsRef, selectedHex }) {
  const map = useMap();
  const linesRef = useRef(new Map()); // hex → L.polyline

  useEffect(() => {
    ensureDebugPanes(map);
    const lines = linesRef.current;
    const tick = () => {
      const trails = frontendTrailsRef.current || {};
      const seen = new Set();
      for (const ac of visibleAircraftRef.current || []) {
        if (!ac.hex || ac.hex === selectedHex) continue;
        const buf = trails[ac.hex];
        if (!buf || buf.length < 2) continue;
        seen.add(ac.hex);
        const positions = buf.map((p) => [p[0], p[1]]);
        let line = lines.get(ac.hex);
        if (line) {
          line.setLatLngs(positions);
        } else {
          line = L.polyline(positions, {
            renderer: _trailsCanvas,
            interactive: false,
            color: "#f59e0b",
            weight: 1.2,
            opacity: 0.5,
            lineCap: "round",
            lineJoin: "round",
          });
          line.addTo(map);
          lines.set(ac.hex, line);
        }
      }
      // Remove trails for aircraft no longer in viewport / selected.
      for (const [hex, line] of lines) {
        if (!seen.has(hex)) {
          line.remove();
          lines.delete(hex);
        }
      }
    };
    tick();
    const id = setInterval(tick, 500);
    return () => {
      clearInterval(id);
      for (const line of lines.values()) line.remove();
      lines.clear();
    };
  }, [map, visibleAircraftRef, frontendTrailsRef, selectedHex]);

  return null;
});

/* ── BasemapLayer: TileLayer with bounded retry on tile load failure.

      Leaflet does not retry a failed tile.  A single 429 or timeout from the
      basemap CDN (Carto's free tier is rate-limited) leaves that square blank
      *permanently* — until something else forces a redraw — which is what
      "sections of the map don't render" looks like.  Retrying with backoff
      recovers the transient case, which is nearly all of it.

      TILE_MAX_RETRIES is deliberately small: if the CDN is genuinely down,
      hammering it makes the rate limiting worse, not better. ── */
const TILE_MAX_RETRIES = 3;
const TILE_RETRY_BASE_MS = 400;

const BasemapLayer = memo(function BasemapLayer({ url }) {
  const retriesRef = useRef(new Map()); // tile src → attempts so far

  const handlers = useMemo(
    () => ({
      tileerror: (e) => {
        const tile = e.tile;
        if (!tile) return;
        // Strip any cache-buster we added so the retry count keys off the
        // real tile identity rather than growing a new entry per attempt.
        const baseSrc = (tile.src || "").split("#tfretry=")[0];
        if (!baseSrc) return;
        const attempts = retriesRef.current.get(baseSrc) ?? 0;
        if (attempts >= TILE_MAX_RETRIES) return;
        retriesRef.current.set(baseSrc, attempts + 1);
        // Exponential backoff. The fragment forces the browser to re-request
        // rather than serve the cached failure, without altering the path the
        // CDN sees.
        setTimeout(() => {
          tile.src = `${baseSrc}#tfretry=${attempts + 1}`;
        }, TILE_RETRY_BASE_MS * 2 ** attempts);
      },
      // Bound the map: without this, a long session panning a large area
      // accumulates one entry per tile ever loaded.
      tileload: (e) => {
        const baseSrc = (e.tile?.src || "").split("#tfretry=")[0];
        if (baseSrc) retriesRef.current.delete(baseSrc);
      },
    }),
    [],
  );

  // Both Carto and OSM require attribution under their terms of use; the map
  // previously suppressed the control entirely.
  const attribution = url.includes("openstreetmap.org")
    ? '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    : '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';

  return (
    <TileLayer
      url={url}
      attribution={attribution}
      eventHandlers={handlers}
      // keepBuffer 4 (default 2): a fast pan otherwise evicts tiles just
      // outside the viewport that are about to be needed again, so they have
      // to be re-fetched — visible as a band of missing map behind the drag.
      keepBuffer={4}
      // Don't request intermediate zoom levels mid-pinch; they are discarded
      // on arrival and only compete for the CDN's rate limit.
      updateWhenZooming={false}
    />
  );
});

/* ── NodeMarkersLayer: SVG CircleMarkers for synthetic nodes + divIcon for the
      real radar node.
      Background reason: 914 DOM divs with drop-shadow filters caused severe
      pan/zoom jank, so the bulk synthetic fleet stays on cheap SVG circles in
      a single overlay.  But the real node is the one the user is actually
      tracking, and a 5 px disc was getting lost under nearby aircraft icons —
      so it gets the larger glowing divIcon (a handful of DOM nodes is fine). ── */
const NodeMarkersLayer = memo(function NodeMarkersLayer({ visibleNodes, onSelectNode }) {
  return visibleNodes.map((n) => {
    const isSynth = n.node_id?.startsWith("synth-");
    if (isSynth) {
      return (
        <CircleMarker
          key={`node-${n.node_id}`}
          center={[n.rx_lat, n.rx_lon]}
          radius={5}
          pathOptions={{ color: "#facc15", fillColor: "#facc15", fillOpacity: 0.55, weight: 1.5 }}
          bubblingMouseEvents={false}
          eventHandlers={{ click: () => onSelectNode(n.node_id) }}
        >
          <Popup>
            <strong>{n.node_id}</strong><br />
            Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
            {n.max_bistatic_range_km != null
              ? <>Bistatic range: {n.max_bistatic_range_km} km<br /></>
              : <>Range: {n.max_range_km} km<br /></>}
            {n.empirical_polygon && n.empirical_polygon.length >= 3
              ? <>Coverage: empirical, {n.empirical_n_points} calibration pts</>
              : <>Coverage: theoretical ({n.empirical_n_points || 0} calibration pts)</>}
          </Popup>
        </CircleMarker>
      );
    }
    return (
      <Marker
        key={`node-${n.node_id}`}
        position={[n.rx_lat, n.rx_lon]}
        icon={nodeIcon}
        zIndexOffset={1000}
        eventHandlers={{ click: () => onSelectNode(n.node_id) }}
      >
        <Popup>
          <strong>{n.node_id}</strong><br />
          Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
          {n.max_bistatic_range_km != null
            ? <>Bistatic range: {n.max_bistatic_range_km} km<br /></>
            : <>Range: {n.max_range_km} km<br /></>}
          {n.empirical_polygon && n.empirical_polygon.length >= 3
            ? <>Coverage: empirical, {n.empirical_n_points} calibration pts</>
            : <>Coverage: theoretical ({n.empirical_n_points || 0} calibration pts)</>}
        </Popup>
      </Marker>
    );
  });
});

/* ── CoverageLayer: memoized — only re-renders when nodes or showCoverage changes ── */
const CoverageLayer = memo(function CoverageLayer({ visibleNodes, showCoverage }) {
  if (!showCoverage) return null;
  return visibleNodes.map((n) => {
    if (n.empirical_polygon && n.empirical_polygon.length >= 3) {
      return (
        <Polygon
          key={`beam-${n.node_id}`}
          positions={n.empirical_polygon}
          pathOptions={{ color: "#22c55e", fillColor: "#22c55e", fillOpacity: 0.12, weight: 1.5 }}
          interactive={false}
        />
      );
    }
    return (
      <Polygon
        key={`beam-${n.node_id}`}
        positions={yagiSectorPositions(
          n.rx_lat, n.rx_lon,
          n.tx_lat, n.tx_lon,
          n.beam_azimuth_deg,
          n.beam_width_deg ?? 42,
          n.max_range_km ?? 50,
          n.max_bistatic_range_km,
        )}
        pathOptions={{ color: "#facc15", fillColor: "#facc15", fillOpacity: 0.1, weight: 1.5, dashArray: "4 4" }}
        interactive={false}
      />
    );
  });
});

/* ── IlluminatorsLayer: TX (broadcast transmitter) positions the nodes use.
      Off by default — multiple nodes often share one illuminator, so we dedupe
      by TX coordinate and render one pink marker per unique transmitter with the
      list of nodes that bounce off it. Only the illuminators our own nodes use,
      not every broadcast tower in range (that would be unreadable clutter). ── */
const IlluminatorsLayer = memo(function IlluminatorsLayer({ visibleNodes, showIlluminators }) {
  if (!showIlluminators) return null;
  const byTx = new Map();
  for (const n of visibleNodes) {
    if (typeof n.tx_lat !== "number" || typeof n.tx_lon !== "number") continue;
    if (Math.abs(n.tx_lat) < 1e-6 && Math.abs(n.tx_lon) < 1e-6) continue;
    const key = `${n.tx_lat.toFixed(4)},${n.tx_lon.toFixed(4)}`;
    if (!byTx.has(key)) byTx.set(key, { lat: n.tx_lat, lon: n.tx_lon, nodes: [] });
    byTx.get(key).nodes.push(n.node_id);
  }
  return [...byTx.entries()].map(([key, tx]) => (
    <CircleMarker
      key={`illum-${key}`}
      center={[tx.lat, tx.lon]}
      radius={6}
      pathOptions={{ color: "#f472b6", fillColor: "#f472b6", fillOpacity: 0.7, weight: 1.5 }}
      bubblingMouseEvents={false}
    >
      <Popup>
        <strong>Illuminator</strong><br />
        {tx.lat.toFixed(4)}, {tx.lon.toFixed(4)}<br />
        Used by {tx.nodes.length} node{tx.nodes.length === 1 ? "" : "s"}: {tx.nodes.join(", ")}
      </Popup>
    </CircleMarker>
  ));
});

/* ── FollowController: when "Follow" is on and an aircraft is selected, keep the
      map centred on its smoothed position. Reads smoothRef (60fps dead-reckoned)
      on a 100ms interval and pans without animation so it tracks smoothly.
      Toggle off (or deselect) to regain free pan/zoom. ── */
const FollowController = memo(function FollowController({ followSelected, selectedHex, smoothRef, onDisengage }) {
  const map = useMap();
  useEffect(() => {
    if (!followSelected || !selectedHex) return;
    const id = setInterval(() => {
      // Truth-only objects live under the namespaced key — without the
      // fallback, Follow silently did nothing for them.
      const sm = smoothRef.current?.[selectedHex]
        ?? smoothRef.current?.[groundTruthKey(selectedHex)];
      if (sm) map.panTo([sm.lat, sm.lon], { animate: false });
    }, 100);
    // A manual drag means the user wants to look elsewhere — disengage follow.
    // panTo({animate:false}) above doesn't fire dragstart, so this only
    // triggers on genuine user panning (zoom stays followed, to look closer).
    const release = () => onDisengage?.();
    map.on("dragstart", release);
    return () => {
      clearInterval(id);
      map.off("dragstart", release);
    };
  }, [followSelected, selectedHex, map, smoothRef, onDisengage]);
  return null;
});

/* ── HashSync: pipes map move/zoom events into a parent callback so the
      enclosing component can mirror lat/lon/z into `location.hash` for
      deep-link sharing. Also draws three range rings (5/10/20 km) around
      the currently-selected aircraft when toggled on — done here because
      it needs `useMap()` to access the live smoothed position via
      `smoothRef`, which other map children already pattern. ── */
const HashSync = memo(function HashSync({ onMove, showRangeRings, selectedHex, smoothRef }) {
  const map = useMapEvents({
    moveend: () => {
      const c = map.getCenter();
      onMove?.({ lat: c.lat, lon: c.lng, z: map.getZoom() });
    },
  });
  // Initial fire so the hash is correct even if the user never pans.
  useEffect(() => {
    const c = map.getCenter();
    onMove?.({ lat: c.lat, lon: c.lng, z: map.getZoom() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!showRangeRings || !selectedHex) return null;
  const sm = smoothRef.current?.[selectedHex]
    ?? smoothRef.current?.[groundTruthKey(selectedHex)];
  if (!sm) return null;
  // Three rings at 5/10/20 km — useful for judging "how far away" without
  // dropping into the detail panel.  Light, dashed strokes keep the rings
  // from competing with the aircraft icon.
  return (
    <>
      {[5000, 10000, 20000].map((r) => (
        <Circle
          key={r}
          center={[sm.lat, sm.lon]}
          radius={r}
          pathOptions={{ color: "#38bdf8", weight: 1, opacity: 0.5, fill: false, dashArray: "4 4" }}
        />
      ))}
    </>
  );
});

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
  /* ── Node-owner view ─────────────────────────────────────────── */
  // Resolved before the feed so `ownerOnly` can pick the server-filtered
  // /ws/aircraft/owner endpoint. Only takes effect once the user is logged in.
  const { user, ownedNodeIds, loading: authLoading } = useAuth();
  const [ownerOnly, setOwnerOnly] = useState(false);
  const ownedSet = useMemo(() => new Set(ownedNodeIds), [ownedNodeIds]);

  /* ── Data feeds ─────────────────────────────────────────────── */
  const {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
    groundTruthMetaRef,
    anomalyHexesRef,
    trailTick,
    groundTruthTick,
    historyRef,
    setPaused: setFeedPaused,
    arcsBufferRef,
    detectionsRef,
  } = useAircraftFeed(ownerOnly);

  const allNodes = useNodes();
  // In owner mode the map shows only the user's own nodes. The aircraft/arc
  // feed is already server-filtered; this filters the node markers/coverage to
  // match. Falls back to all nodes when the toggle is off.
  const nodes = useMemo(
    () => (ownerOnly ? allNodes.filter((n) => ownedSet.has(n.node_id)) : allNodes),
    [allNodes, ownerOnly, ownedSet],
  );
  // Per-node geometry lookup for the client-side bistatic-arc rebuilder.
  // Mirrors `nodes` content but keyed by node_id for O(1) access inside the
  // DetectionArcs render tick.  Kept on a ref so the tick can read fresh
  // values without re-running the effect on every nodes-poll cycle (30 s).
  const nodesByIdRef = useRef({});
  useEffect(() => {
    const m = {};
    for (const n of nodes) m[n.node_id] = n;
    nodesByIdRef.current = m;
  }, [nodes]);

  /* ── Local UI state ─────────────────────────────────────────── */
  // URL-hash deep-link state — parsed once at mount.  Anything we find here
  // overrides the user's persisted preferences, so a teammate sharing a
  // link sees the exact view that was sent.
  const initialHash = useMemo(() => parseHash(), []);
  // Single-sourced from useUrlHashState — this was a character-for-character
  // inline copy of decodeLayers, maintained twice.
  const initialLayers = useMemo(
    () => (initialHash.layers ? decodeLayers(initialHash.layers) : null),
    [initialHash],
  );

  const [displayAircraft, setDisplayAircraft] = useState([]);
  const [showCoverage, setShowCoverage] = usePersistedState("tf.layer.coverage", initialLayers?.coverage ?? false);
  const [showTrails, setShowTrails] = usePersistedState("tf.layer.trails", initialLayers?.trails ?? true);
  // Default GT on for testmap/staging-testmap (simulation demo); off on map.* and staging-map.* (real only)
  const [showGroundTruth, setShowGroundTruth] = usePersistedState("tf.layer.groundTruth", initialLayers?.groundTruth ?? !defaultsGroundTruthOff);
  const [showLabels, setShowLabels] = usePersistedState("tf.layer.labels", initialLayers?.labels ?? true);
  const [selectedHex, setSelectedHex] = useState(initialHash.hex ?? null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [paused, setPaused] = useState(false);
  // Read by the 60 fps loop (which has [] deps and can't see state) so pause
  // actually freezes the display instead of racing the playback slider.
  const pausedLoopRef = useRef(false);
  // Controlled slider position for the playback bar; null = live end.
  const [seekIndex, setSeekIndex] = useState(null);
  const [sidebarCollapsed, setSidebarCollapsed] = usePersistedState("tf.sidebar.collapsed", false);
  const [viewport, setViewport] = useState(null);
  const [showAnomaliesOnly, setShowAnomaliesOnly] = useState(false);
  const [showIlluminators, setShowIlluminators] = usePersistedState("tf.layer.illuminators", initialLayers?.illuminators ?? false);
  const [colorByAlt, setColorByAlt] = usePersistedState("tf.layer.colorByAlt", initialLayers?.colorByAlt ?? false);
  const [followSelected, setFollowSelected] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [showStats, setShowStats] = usePersistedState("tf.layer.stats", initialLayers?.stats ?? true);
  const [showRangeRings, setShowRangeRings] = usePersistedState("tf.layer.rangeRings", initialLayers?.rangeRings ?? false);
  // Beam-gap diagnostic defaults OFF: it draws one line per (aircraft, node)
  // pair, so a metro-scoped fleet whose nodes all cover the same airspace turns
  // the map into a thicket. Still available from the "Beam gaps" toolbar button
  // and the `b` URL-hash layer.
  // Storage key is versioned (.v2) so the new default reaches anyone who
  // already has the old `tf.layer.inBeamDiag: true` persisted in localStorage —
  // without it, every existing user keeps seeing the lines.
  const [showInBeamDiag, setShowInBeamDiag] = usePersistedState("tf.layer.inBeamDiag.v2", initialLayers?.inBeamDiag ?? false);
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  // Enthusiast filters: altitude band (FL, hundreds of ft), speed floor, type.
  const [filters, setFilters] = usePersistedState("tf.filters", { minFl: "", maxFl: "", minGs: "", type: "all" });
  // List sort & pinning. Pinned hex codes always render at the top of the
  // list and survive page reloads.
  const [sortMode, setSortMode] = usePersistedState("tf.list.sort", "altitude");
  const [pinned, setPinned] = usePersistedState("tf.list.pinned", []);
  const pinnedSet = useMemo(() => new Set(pinned || []), [pinned]);
  const togglePinned = useCallback((hex) => {
    if (!hex) return;
    setPinned((arr) => {
      const list = Array.isArray(arr) ? arr : [];
      return list.includes(hex) ? list.filter((h) => h !== hex) : [...list, hex];
    });
  }, [setPinned]);
  // Audio alert for emergency squawks (7500/7600/7700). One chime per
  // (hex, squawk) until the user clears the cache.
  const [soundOn, setSoundOn] = usePersistedState("tf.sound.emergency", true);
  // User geolocation — opt-in. Drives the "you are here" marker and the
  // distance column in the list panel.
  const [userLoc, setUserLoc] = useState(null); // { lat, lon } | null
  // Tile theme — voyager (default dark-ish), positron (light), osm (classic).
  const [tileTheme, setTileTheme] = usePersistedState("tf.tile.theme", "voyager");

  const animationFrameRef = useRef(null);
  const fixesRef = useRef({});   // hex → last server fix
  const smoothRef = useRef({});  // hex → { lat, lon, track } — smoothed render position
  const prevTsRef = useRef(null);
  const svgElemsRef = useRef({}); // hex → cached SVG DOM element (avoids querySelector every frame)
  const svgMissRef = useRef({});  // hex → retry-after ts for DOM lookup misses (negative cache)
  const rafFrameRef = useRef(0);  // throttle React re-renders to ~2fps (position/rotation at 60fps via direct L.Marker/DOM)
  const markerRegistryRef = useRef(new Map()); // hex → L.Marker for imperative 60fps setLatLng
  const latLngCacheRef    = useRef({});         // hex → L.LatLng — mutated in place to avoid per-frame allocation
  // Per-hex trail of smoothed lat/lon samples taken from the 60fps DR loop.
  // For arc-only tracks the backend's recent_positions stays at 1 point
  // (append_track_history dedupes positions < 5 m apart, and the arc midpoint
  // doesn't change between detections).  Sampling smoothRef into a frontend
  // buffer at ~2 Hz lets us draw a trail behind the dead-reckoned position
  // without needing backend changes.  Bounded to 60 samples per hex (30 s).
  const frontendTrailsRef = useRef({});  // hex → Array<[lat, lon, ts_sec]>
  const lastTrailSampleRef = useRef({}); // hex → last sample timestamp (ms)
  // One bundle over all eight per-object stores, so the prune paths are
  // three callers of trackStores.forgetTrack instead of three hand-kept
  // subsets (see map/trackStores.ts for the history).
  const allStoresRef = useRef(null);
  if (!allStoresRef.current) {
    allStoresRef.current = {
      get fixes() { return fixesRef.current; },
      get smooth() { return smoothRef.current; },
      get svgElems() { return svgElemsRef.current; },
      get svgMiss() { return svgMissRef.current; },
      get latLng() { return latLngCacheRef.current; },
      get trails() { return frontendTrailsRef.current; },
      get lastTrailSample() { return lastTrailSampleRef.current; },
      get markerRegistry() { return markerRegistryRef.current; },
    };
  }

  /* ── Record server fixes when new WS data arrives ───────────── */
  useEffect(() => {
    const now = Date.now();
    // Shared spherical helper — this held one of the map's five distance
    // implementations (equirect at 111.32); <1% apart, but one is enough.
    const distKm = distanceKm;
    for (const ac of aircraft) {
      if (!validLatLon(ac.lat, ac.lon)) continue;
      const prev = fixesRef.current[ac.hex];
      const posChanged = !prev || prev._fixLat !== ac.lat || prev._fixLon !== ac.lon;

      // Teleport guard: if a new fix arrives implausibly far from where the
      // icon is currently being rendered (smoothed/dead-reckoned), don't
      // glide across the gap (the exponential smoother would otherwise
      // animate a multi-km "supersonic swoosh" over ~2-3 s).  Instead snap
      // smoothRef directly to the new fix and drop the per-hex trail so the
      // polyline doesn't connect the old and new positions through the gap.
      // A jump is implausible when it exceeds (max aircraft ground speed)
      // × elapsed since the previous fix, plus a small tolerance.
      if (posChanged && prev) {
        const dtSec = Math.max((now - (prev._fixTs ?? now)) / 1000, 0.1);
        // 1.0 km/s ≈ Mach 3 — well above any civil/commercial target we
        // expect.  Anything beyond this is a solver/association glitch.
        const maxPlausibleKm = 0.6 + 1.0 * dtSec;
        const sm = smoothRef.current[ac.hex];
        const refLat = sm?.lat ?? prev._fixLat;
        const refLon = sm?.lon ?? prev._fixLon;
        const jumpKm = distKm(refLat, refLon, ac.lat, ac.lon);
        if (jumpKm > maxPlausibleKm) {
          snapTrack(ac.hex, allStoresRef.current, ac.lat, ac.lon, ac.track);
        }
      }

      fixesRef.current[ac.hex] = {
        ...ac,
        _key: ac.hex,
        _fixLat: ac.lat,
        _fixLon: ac.lon,
        // Only reset the position-anchor timestamp when the fix actually moved.
        // If the server re-broadcasts the same lat/lon (between solve cycles),
        // preserve _fixTs so dead-reckoning keeps projecting forward.
        _fixTs: posChanged ? now : (prev._fixTs ?? now),
        _updatedAt: now,
      };
    }
    // Drop stale entries no longer in the feed (skip truth-only — managed by their own effect)
    sweepStaleRadar(allStoresRef.current, now, STALE_AIRCRAFT_MS);
  }, [aircraft]);

  /* ── Continuous 60fps glide loop (dead-reckoning + exponential smoothing) ── */
  useEffect(() => {
    const DEG_PER_M = 1 / 111_320;
    const KNOTS_TO_MS = 0.514444;
    // Smoothing time constant: lower = snappier, higher = more glide (seconds)
    const TAU = 0.55;

    const tick = (ts) => {
      // Paused: freeze everything.  The loop used to keep dead-reckoning and
      // pushing setDisplayAircraft at 2 Hz regardless, so a frame chosen on
      // the playback slider was clobbered by live data within ~500 ms —
      // playback was effectively non-functional.
      if (pausedLoopRef.current) {
        prevTsRef.current = ts;
        animationFrameRef.current = requestAnimationFrame(tick);
        return;
      }
      const dt = prevTsRef.current !== null ? Math.min((ts - prevTsRef.current) / 1000, 0.1) : 0;
      prevTsRef.current = ts;
      const alpha = dt > 0 ? 1 - Math.exp(-dt / TAU) : 1;

      const now = Date.now();
      const fixes = fixesRef.current;
      for (const fix of Object.values(fixes)) {
        // Store key, not the display hex — ground-truth objects are namespaced
        // so a radar track sharing their ICAO hex can't overwrite them.
        const key = fix._key || fix.hex;
        // Arc-only tracks get a much shorter dead-reckoning window: their
        // backend position is the arc midpoint, so a 60 s glide walks them
        // 7–11 km off the measured locus (see ARC_DR_MAX_S in constants.ts).
        const drCapS =
          fix.position_source === POSITION_SOURCE_ARC_ONLY ? ARC_DR_MAX_S : 60;
        const elapsed = Math.min((now - fix._fixTs) / 1000, drCapS);
        const gs = fix.gs || 0;

        // 1. Dead-reckon the physics target
        let targetLat = fix._fixLat;
        let targetLon = fix._fixLon;
        if (elapsed > 0 && gs > 0) {
          const gs_m_s = gs * KNOTS_TO_MS;
          const track_rad = (fix.track || 0) * (Math.PI / 180);
          const cos_lat = Math.cos(fix._fixLat * (Math.PI / 180)) || 1e-9;
          targetLat = fix._fixLat + gs_m_s * Math.cos(track_rad) * DEG_PER_M * elapsed;
          targetLon = fix._fixLon + (gs_m_s * Math.sin(track_rad)) / (111_320 * cos_lat) * elapsed;
        }

        // 2. Exponential smoothing toward the target (glide / inertia effect)
        const prev = smoothRef.current[key];
        const sLat = prev ? prev.lat + (targetLat - prev.lat) * alpha : targetLat;
        const sLon = prev ? prev.lon + (targetLon - prev.lon) * alpha : targetLon;

        // Smooth heading with wrap-around handling
        const targetTrack = fix.track || 0;
        const prevTrack = prev ? prev.track : targetTrack;
        const dTrack = ((targetTrack - prevTrack + 540) % 360) - 180;
        const sTrack = (prevTrack + dTrack * alpha + 360) % 360;

        // Mutate smooth entry in place — avoids 412 short-lived object creations per frame
        const sm = smoothRef.current[key];
        if (sm) { sm.lat = sLat; sm.lon = sLon; sm.track = sTrack; }
        else     smoothRef.current[key] = { lat: sLat, lon: sLon, track: sTrack };

        // Update rotation directly on the DOM — avoids setIcon() every frame.
        // Ground-truth objects render on a canvas layer with no divIcon, and
        // their store key (gt:<hex>) never matches the ac-hex-<hex> class —
        // querying for them every frame was a permanent document-wide
        // selector miss, ~30k/s on testmap.  Arc-only tracks are skipped for
        // the same reason: they render no plane marker at all, so the lookup
        // could never succeed.  Misses for real markers are negative-cached
        // briefly: an aircraft outside the viewport or filtered out has no
        // DOM node until it re-enters.
        if (!fix._isTruth && fix.position_source !== POSITION_SOURCE_ARC_ONLY) {
          let svgEl = svgElemsRef.current[key];
          if (!svgEl || !svgEl.isConnected) {
            svgEl = null;
            delete svgElemsRef.current[key];
            const retryAt = svgMissRef.current[key] || 0;
            if (now >= retryAt) {
              svgEl = document.querySelector(`.ac-hex-${CSS.escape(key)} svg`);
              if (svgEl) {
                svgElemsRef.current[key] = svgEl;
                delete svgMissRef.current[key];
              } else {
                svgMissRef.current[key] = now + 2000;
              }
            }
          }
          if (svgEl) svgEl.style.transform = `rotate(${sTrack.toFixed(1)}deg)`;
        }

        // Imperative Leaflet position — reuse cached L.LatLng and call marker.update() directly
        // to avoid per-frame LatLng + event-object allocations (was ~25k allocs/s at 60fps×412).
        const marker = markerRegistryRef.current.get(key);
        if (marker) {
          let ll = latLngCacheRef.current[key];
          if (ll) { ll.lat = sLat; ll.lng = sLon; }
          else { ll = L.latLng(sLat, sLon); latLngCacheRef.current[key] = ll; }
          // Always bind our cached LatLng to the marker — when React re-renders
          // an AircraftMarker (altitude band change, selection, etc.), the new
          // L.Marker has a fresh _latlng that isn't our cached object.
          if (marker._latlng !== ll) marker._latlng = ll;
          marker.update();
        }

        // Sample the smoothed position into a per-hex trail buffer at ~2 Hz
        // (every 500 ms).  This is the source for the trail polyline on
        // arc-only tracks whose backend recent_positions stays at 1 point
        // because the arc midpoint doesn't move between detections.
        const lastSample = lastTrailSampleRef.current[key] || 0;
        if (now - lastSample >= 500) {
          lastTrailSampleRef.current[key] = now;
          let trail = frontendTrailsRef.current[key];
          if (!trail) { trail = []; frontendTrailsRef.current[key] = trail; }
          trail.push([sLat, sLon, now / 1000]);
          if (trail.length > 60) trail.shift();
        }
      }

      // Build React display array at 2fps only — avoids ~25k spread-object allocations/s at 60fps.
      rafFrameRef.current = (rafFrameRef.current + 1) % 30;
      if (rafFrameRef.current === 0) {
        // Truth objects age out here (render-loop driven) because their
        // arrival-driven prune never fires once the feed stops — stale blue
        // dots dead-reckoned forever.
        sweepStaleGroundTruthFixes(allStoresRef.current, now, GT_FEED_STALE_MS);
        const arr = [];
        for (const fix of Object.values(fixes)) {
          const sm = smoothRef.current[fix._key || fix.hex];
          if (!sm) continue;
          arr.push({ ...fix, lat: sm.lat, lon: sm.lon, track: sm.track });
        }
        setDisplayAircraft(arr);
      }
      animationFrameRef.current = requestAnimationFrame(tick);
    };

    animationFrameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrameRef.current);
  }, []);  

  /* ── Derived: radar-detected only (exclude pure ADS-B not seen by radar) ── */
  const radarAircraft = useMemo(
    () => {
      let base = displayAircraft.filter((ac) => ac.position_source || ac.multinode);
      if (showAnomaliesOnly) base = base.filter((ac) => ac.is_anomalous);
      // Enthusiast filters: altitude (flight level = alt_baro/100), ground speed, type.
      const minFl = filters.minFl === "" ? null : Number(filters.minFl);
      const maxFl = filters.maxFl === "" ? null : Number(filters.maxFl);
      const minGs = filters.minGs === "" ? null : Number(filters.minGs);
      base = base.filter((ac) => {
        const fl = (ac.alt_baro ?? 0) / 100;
        if (minFl != null && fl < minFl) return false;
        if (maxFl != null && fl > maxFl) return false;
        if (minGs != null && (ac.gs ?? 0) < minGs) return false;
        if (filters.type === "drone" && ac.target_class !== "drone") return false;
        if (filters.type === "aircraft" && ac.target_class === "drone") return false;
        if (filters.type === "multinode" && !(ac.multinode || ac.position_source === "multinode_solve")) return false;
        return true;
      });
      return base;
    },
    [displayAircraft, showAnomaliesOnly, filters],
  );

  const anomalyCount = useMemo(
    () => displayAircraft.filter((ac) => (ac.position_source || ac.multinode) && ac.is_anomalous).length,
    [displayAircraft],
  );

  /* ── Derived: truth-only aircraft ───────────────────────────── */
  /* ── Feed ground-truth objects into fixesRef so the 60fps loop dead-reckons them ── */
  useEffect(() => {
    const now = Date.now();
    const activeGtKeys = applyGroundTruthFixes(
      fixesRef.current, groundTruthRef.current, groundTruthMetaRef.current, now,
    );
    pruneGroundTruthFixes(allStoresRef.current, activeGtKeys, now, GT_PRUNE_GRACE_MS);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groundTruthTick]);

  // trailTick still drives trail rendering; groundTruthTick drives this expensive
  // recompute only when the ground-truth dataset is actually replaced (~1Hz).
  // Positions are now read from displayAircraft (60fps smoothed) rather than
  // raw groundTruthRef so ground-truth dots move continuously like radar tracks.
  const truthOnlyAircraft = useMemo(
    () => displayAircraft.filter((ac) => ac._isTruth),
    [displayAircraft],
  );


  /* ── Derived: viewport culling ──────────────────────────────── */
  const filteredAircraft = useMemo(() => {
    if (!searchQuery.trim()) return radarAircraft;
    const q = searchQuery.trim().toLowerCase();
    return radarAircraft.filter(
      (ac) => (ac.hex || "").toLowerCase().includes(q) || (ac.flight || "").toLowerCase().includes(q),
    );
  }, [radarAircraft, searchQuery]);

  const visibleAircraft = useMemo(
    () => filteredAircraft.filter((ac) => ac.hex === selectedHex || isAircraftInViewport(ac, viewport)),
    [filteredAircraft, selectedHex, viewport],
  );
  // Ref mirrors for the imperative layers: their effects read data inside
  // their own tick instead of keying on these 2 Hz array identities, which
  // used to tear every Leaflet object down twice a second.
  const visibleAircraftRef = useRef(visibleAircraft);
  const radarAircraftRef = useRef(radarAircraft);
  useEffect(() => { visibleAircraftRef.current = visibleAircraft; }, [visibleAircraft]);
  useEffect(() => { radarAircraftRef.current = radarAircraft; }, [radarAircraft]);

  // No viewport filter — the L.canvas renderer handles off-screen dots natively.
  // Removing the filter means:
  //  1. All truth aircraft appear IMMEDIATELY on toggle (no blank-until-pan).
  //  2. Every pan no longer re-triggers this memo + GroundTruthCanvasLayer.useEffect.
  const visibleTruthOnlyAircraft = useMemo(
    () => showGroundTruth ? truthOnlyAircraft : [],
    [showGroundTruth, truthOnlyAircraft],
  );

  const visibleNodes = useMemo(
    () => nodes.filter((node) => isPointInViewport(node.rx_lat, node.rx_lon, viewport, 0.3)),
    [nodes, viewport],
  );

  /* The verification overlay only means anything for real nodes —
     synthetic simulation nodes have no ADS-B truth to compare against.
     Null hides the layer. */
  const verificationNodeId = useMemo(() => {
    if (!selectedNodeId) return null;
    const node = nodes.find((n) => n.node_id === selectedNodeId);
    return node && !node.is_synthetic ? selectedNodeId : null;
  }, [nodes, selectedNodeId]);

  /* ── Derived: trail for selected aircraft ───────────────────── */
  const visibleTrailEntries = useMemo(() => {
    if (!selectedHex) return [];
    return Object.entries(trailsRef.current).filter(
      ([hex, positions]) => hex === selectedHex && positions.some((p) => isPointInViewport(p[0], p[1], viewport)),
    );
  }, [selectedHex, trailTick, viewport]);

  const selectedTrailPositions = useMemo(() => {
    if (!selectedHex) return [];
    // Start from backend's recent_positions if present.
    const pts = [];
    if (visibleTrailEntries.length) {
      const [, positions] = visibleTrailEntries[0];
      for (const p of sampleTrailPositions(positions)) pts.push([p[0], p[1]]);
    }
    // Merge in the frontend trail (per-hex smoothed samples at 2 Hz).
    // For arc-only tracks the backend recent_positions stays at 1 point
    // because the arc midpoint doesn't move between detections, so without
    // this fallback the selected aircraft would render no trail at all.
    // Skip front samples already covered by the backend tail to avoid
    // doubling up on the very recent positions.
    const frontTrail =
      frontendTrailsRef.current[selectedHex] ?? frontendTrailsRef.current[groundTruthKey(selectedHex)];
    if (frontTrail && frontTrail.length) {
      const lastBack = pts[pts.length - 1];
      for (const [lat, lon] of frontTrail) {
        if (lastBack && Math.abs(lastBack[0] - lat) < 1e-5 && Math.abs(lastBack[1] - lon) < 1e-5) continue;
        pts.push([lat, lon]);
      }
    }
    // smoothRef is updated at 60fps (vs displayedAircraftRef which is only 2fps)
    // so the trail tip connects exactly to the current smoothed position.
    // A truth-only selection has no radar entry under its bare hex; fall back
    // to the namespaced ground-truth key so its trail tip still tracks.
    const animated = smoothRef.current[selectedHex] ?? smoothRef.current[groundTruthKey(selectedHex)];
    if (animated?.lat && animated?.lon) {
      const last = pts[pts.length - 1];
      if (!last || Math.abs(last[0] - animated.lat) > 0.00001 || Math.abs(last[1] - animated.lon) > 0.00001) {
        pts.push([animated.lat, animated.lon]);
      }
    }
    return pts;
  }, [selectedHex, visibleTrailEntries]);

  const selectedAc = selectedHex
    ? radarAircraft.find((ac) => ac.hex === selectedHex) || truthOnlyAircraft.find((ac) => ac.hex === selectedHex)
    : null;

  // Per-solve history for the selected MLAT track (debug): fetched once per
  // selection + refreshed on the backend's ~30 s recording cadence.  Tagged
  // with the hex it was fetched for so a selection change never shows the
  // previous track's solves while the new fetch is in flight.
  const selectedMnHex =
    selectedAc?.position_source === "multinode_solve" ? selectedAc.hex : null;
  const [mlatHistory, setMlatHistory] = useState(null);
  useEffect(() => {
    if (!selectedMnHex) {
      setMlatHistory(null);
      return;
    }
    let cancelled = false;
    const load = () => {
      fetchMlatHistory(selectedMnHex).then((d) => {
        if (!cancelled && d && d.hex === selectedMnHex) setMlatHistory(d);
      });
    };
    load();
    const interval = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedMnHex]);

  // Nodes with a live detection of the selected simulated object — read from
  // the detection-presence oracle (per-aircraft signals ∪ the detecting_nodes
  // feed key).  trailTick advances on every ingest, so this refreshes at the
  // feed cadence without its own timer.
  const selectedTruthDetectingNodes = useMemo(() => {
    if (!selectedAc?._isTruth) return [];
    return detectingNodeIdsFor(
      detectionsRef.current, selectedAc.hex, Date.now(), ARC_TOTAL_LIFE_MS,
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAc, trailTick]);

  /* ── Side-effects ───────────────────────────────────────────── */
  useEffect(() => {
    if (!showGroundTruth && selectedHex && truthOnlyAircraft.some((ac) => ac.hex === selectedHex)) {
      setSelectedHex(null);
    }
  }, [showGroundTruth, selectedHex, truthOnlyAircraft]);

  /* ── Callbacks ──────────────────────────────────────────────── */
  const handleViewportChange = useCallback((next) => {
    setViewport((prev) => {
      if (prev && Math.abs(prev.north - next.north) < 0.01 && Math.abs(prev.south - next.south) < 0.01 && Math.abs(prev.east - next.east) < 0.01 && Math.abs(prev.west - next.west) < 0.01) return prev;
      return next;
    });
  }, []);

  function handleTogglePause() {
    const next = !paused;
    setPaused(next);
    setFeedPaused(next);
    pausedLoopRef.current = next;
    setSeekIndex(next ? historyRef.current.length - 1 : null);
  }

  function handleHistorySeek(index) {
    if (index >= 0 && index < historyRef.current.length) {
      setSeekIndex(index);
      setDisplayAircraft(historyRef.current[index].aircraft);
    }
  }

  const handleSelectAircraft = useCallback((hex, shouldFocus = true) => {
    setSelectedHex((prev) => {
      const next = prev === hex ? null : hex;
      // Only zoom when selecting a new aircraft, not when deselecting.
      // Arc clicks pass shouldFocus=false so the camera stays put — yanking
      // the viewport on every trail click is disorienting.
      if (next !== null && shouldFocus) setFocusNonce((n) => n + 1);
      return next;
    });
  }, []);

  const handleSelectNode = useCallback((nodeId) => {
    setSelectedNodeId((prev) => (prev === nodeId ? null : nodeId));
  }, []);

  const handleMapClick = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedHex(null);
  }, []);

  /* ── URL-hash mirror ─────────────────────────────────────────
     One throttled writer fed by both map move/zoom events and
     toggle/selection changes. Lets users share a deep-link URL that
     re-creates the same view + selected aircraft on load. */
  const writeHash = useHashWriter();
  const mapPosRef = useRef({ lat: initialHash.lat, lon: initialHash.lon, z: initialHash.z });
  const handleMapMove = useCallback((p) => {
    mapPosRef.current = p;
    writeHash({
      lat: p.lat, lon: p.lon, z: p.z,
      hex: selectedHex,
      layers: encodeLayers({
        coverage: showCoverage, labels: showLabels, trails: showTrails,
        groundTruth: showGroundTruth, illuminators: showIlluminators,
        colorByAlt, stats: showStats, rangeRings: showRangeRings,
        inBeamDiag: showInBeamDiag,
      }),
    });
  }, [writeHash, selectedHex, showCoverage, showLabels, showTrails, showGroundTruth, showIlluminators, colorByAlt, showStats, showRangeRings, showInBeamDiag]);

  // Push hash when selection or toggles change without waiting for a pan.
  useEffect(() => {
    const p = mapPosRef.current;
    writeHash({
      lat: p.lat, lon: p.lon, z: p.z,
      hex: selectedHex,
      layers: encodeLayers({
        coverage: showCoverage, labels: showLabels, trails: showTrails,
        groundTruth: showGroundTruth, illuminators: showIlluminators,
        colorByAlt, stats: showStats, rangeRings: showRangeRings,
        inBeamDiag: showInBeamDiag,
      }),
    });
  }, [writeHash, selectedHex, showCoverage, showLabels, showTrails, showGroundTruth, showIlluminators, colorByAlt, showStats, showRangeRings, showInBeamDiag]);

  /* ── Keyboard shortcuts ─────────────────────────────────────
     Single-letter bindings.  Suppressed while typing in inputs so the
     search box still works.  See ShortcutHelp for the user-facing list. */
  const searchInputRef = useRef(null);
  const exportSelectedTrail = useCallback(() => {
    if (!selectedHex) { toast("Select an aircraft first", { tone: "warn" }); return; }
    const ac = (radarAircraft || []).find((a) => a.hex === selectedHex);
    if (!ac) {
      // Truth-only selection: no radar track, but the ground-truth trail is
      // still exportable.  This used to return silently — a dead keystroke.
      const gtRows = groundTruthRef.current?.[selectedHex] || [];
      if (!gtRows.length) { toast("No trail data for this object", { tone: "warn" }); return; }
      const gtCsv = trailToCsv(selectedHex, "", gtRows);
      downloadCsv(`trail-${selectedHex}-${Date.now()}.csv`, gtCsv);
      toast(`Exported ${gtRows.length} ground-truth points`, { tone: "success" });
      return;
    }
    // Prefer the backend-fed trail (alt + ms timestamps); fall back to the
    // frontend smoothed trail (no altitude) when no backend points exist.
    const rows =
      (trailsRef.current && trailsRef.current[ac.hex]) ||
      (frontendTrailsRef.current && frontendTrailsRef.current[ac.hex]) ||
      [];
    if (!rows.length) { toast("No trail data yet", { tone: "warn" }); return; }
    const csv = trailToCsv(ac.hex, ac.flight, rows);
    downloadCsv(`trail-${ac.hex}-${Date.now()}.csv`, csv);
    toast(`Exported ${rows.length} points`, { tone: "success" });
  }, [selectedHex, radarAircraft, trailsRef]);

  const exportAllTrails = useCallback(() => {
    const csv = trailsToBulkCsv(radarAircraft || [], trailsRef.current || {});
    downloadCsv(`tower-finder-trails-${Date.now()}.csv`, csv);
    toast(`Exported ${radarAircraft?.length || 0} aircraft`, { tone: "success" });
  }, [radarAircraft, trailsRef]);

  const shareLink = useCallback(() => {
    // The hash already encodes view + layers + selection — `location.href`
    // is the canonical share URL.
    copyToClipboard(window.location.href, "Link copied to clipboard");
  }, []);

  const locateMe = useCallback(() => {
    if (!navigator.geolocation) { toast("Geolocation unavailable", { tone: "error" }); return; }
    toast("Locating…", { tone: "info" });
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setUserLoc({ lat: pos.coords.latitude, lon: pos.coords.longitude });
        toast("Location set", { tone: "success" });
      },
      (err) => {
        toast(`Location: ${err.message || "denied"}`, { tone: "error" });
      },
      { enableHighAccuracy: false, maximumAge: 60_000, timeout: 8_000 },
    );
  }, []);

  // Emergency squawk audio + visual nudge. Runs once per render-tick;
  // dedupes internally so the same aircraft only chimes once.
  useEffect(() => {
    const fired = checkEmergencySquawks(radarAircraft || [], soundOn);
    for (const ev of fired) {
      toast(`⚠ Squawk ${ev.squawk} — ${ev.hex.toUpperCase()}`, { tone: "error", durationMs: 6000 });
    }
  }, [radarAircraft, soundOn]);
  // Forget alerted-cache when the operator pauses/resumes so a long-running
  // emergency re-announces after a deliberate reset.
  useEffect(() => { if (!paused) resetEmergencyAlertCache(); }, [paused]);

  const shortcutMap = useMemo(() => ({
    "?": () => setShowShortcutHelp((v) => !v),
    "/": (e) => { e.preventDefault?.(); searchInputRef.current?.focus?.(); },
    "Escape": () => {
      if (showShortcutHelp) { setShowShortcutHelp(false); return; }
      if (searchQuery) { setSearchQuery(""); return; }
      setSelectedHex(null);
    },
    " ": () => handleTogglePause(),
    f: () => setShowFilters((v) => !v),
    l: () => setShowLabels((v) => !v),
    t: () => setShowTrails((v) => !v),
    c: () => setShowCoverage((v) => !v),
    i: () => setShowIlluminators((v) => !v),
    g: () => setShowGroundTruth((v) => !v),
    a: () => setColorByAlt((v) => !v),
    s: () => setShowStats((v) => !v),
    r: () => setShowRangeRings((v) => !v),
    x: () => exportSelectedTrail(),
    X: () => exportAllTrails(),
    p: () => { if (selectedHex) { togglePinned(selectedHex); toast(pinnedSet.has(selectedHex) ? "Unpinned" : "Pinned"); } },
    m: () => locateMe(),
    n: () => { setSoundOn((v) => { toast(v ? "Sound off" : "Sound on"); return !v; }); },
  }), [showShortcutHelp, searchQuery, exportSelectedTrail, exportAllTrails, locateMe, selectedHex, togglePinned, pinnedSet, setSoundOn, setShowLabels, setShowTrails, setShowCoverage, setShowIlluminators, setShowGroundTruth, setColorByAlt, setShowStats, setShowRangeRings]);
  useKeyboardShortcuts(shortcutMap);

  function computeError(hex, ac) {
    const gtHex = ac.ground_truth_hex || hex;
    // Compare against the SMOOTHED ground-truth position — the same one the
    // yellow error line on the map is drawn from — so the panel number and
    // the drawn line agree.  Comparing dead-reckoned radar against the raw
    // last GT point added up to ~0.25 km of pure timing skew at 480 kt, and
    // used a third flat-earth constant (111.0) 0.3% off the shared helper.
    const sm = smoothRef.current?.[groundTruthKey(gtHex)];
    let gtLat, gtLon;
    if (sm) {
      gtLat = sm.lat;
      gtLon = sm.lon;
    } else {
      const gtTrail = groundTruthRef.current[gtHex];
      if (!gtTrail || !gtTrail.length) return null;
      const last = gtTrail[gtTrail.length - 1];
      gtLat = last[0];
      gtLon = last[1];
    }
    // Arc-only tracks: shortest distance from GT to the measured locus (the
    // same rule MatchedGroundTruthLayer draws) — the midpoint is a display
    // convention, not a position estimate.
    if (ac.position_source === POSITION_SOURCE_ARC_ONLY) {
      const near = arcNearestPoint(
        ac, nodesByIdRef.current?.[ac.node_id], gtLat, gtLon,
      );
      if (near) return near.distKm;
    }
    return distanceKm(ac.lat, ac.lon, gtLat, gtLon);
  }

  function formatSecondsAgo(ts) {
    const sec = Math.round((Date.now() - ts) / 1000);
    return sec <= 0 ? "now" : `-${sec}s`;
  }

  /* ── Render ─────────────────────────────────────────────────── */
  return (
    <div className={"live-map-container" + (showLabels ? "" : " tf-hide-error-labels")}>
      <Toolbar
        connected={connected}
        paused={paused}
        aircraftCount={radarAircraft.length + (showGroundTruth ? truthOnlyAircraft.length : 0)}
        anomalyCount={anomalyCount}
        showCoverage={showCoverage}
        showLabels={showLabels}
        showTrails={showTrails}
        showGroundTruth={showGroundTruth}
        showAnomaliesOnly={showAnomaliesOnly}
        showIlluminators={showIlluminators}
        colorByAlt={colorByAlt}
        followSelected={followSelected}
        showFilters={showFilters}
        showStats={showStats}
        showRangeRings={showRangeRings}
        showInBeamDiag={showInBeamDiag}
        soundOn={soundOn}
        tileTheme={tileTheme}
        hasUserLoc={!!userLoc}
        onToggleCoverage={() => setShowCoverage((v) => !v)}
        onToggleLabels={() => setShowLabels((v) => !v)}
        onToggleTrails={() => setShowTrails((v) => !v)}
        onToggleGroundTruth={() => setShowGroundTruth((v) => !v)}
        onToggleAnomaliesOnly={() => setShowAnomaliesOnly((v) => !v)}
        onToggleIlluminators={() => setShowIlluminators((v) => !v)}
        onToggleColorByAlt={() => setColorByAlt((v) => !v)}
        onToggleFollow={() => setFollowSelected((v) => !v)}
        onToggleFilters={() => setShowFilters((v) => !v)}
        onToggleStats={() => setShowStats((v) => !v)}
        onToggleRangeRings={() => setShowRangeRings((v) => !v)}
        onToggleInBeamDiag={() => setShowInBeamDiag((v) => !v)}
        onToggleSound={() => setSoundOn((v) => !v)}
        onCycleTheme={() => setTileTheme((t) => t === "voyager" ? "positron" : t === "positron" ? "osm" : "voyager")}
        onShare={shareLink}
        onLocate={locateMe}
        onExportAll={exportAllTrails}
        onShowHelp={() => setShowShortcutHelp(true)}
        onTogglePause={handleTogglePause}
        onFit={() => setFocusNonce((n) => n + 1)}
      />

      <div className="live-map-body">
        <AircraftListPanel
          allAircraft={radarAircraft}
          truthOnly={showGroundTruth ? truthOnlyAircraft : []}
          selectedHex={selectedHex}
          onSelect={handleSelectAircraft}
          collapsed={sidebarCollapsed}
          onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          searchInputRef={searchInputRef}
          sortMode={sortMode}
          onSortChange={setSortMode}
          pinned={pinnedSet}
          onTogglePin={togglePinned}
          userLoc={userLoc}
        />

        <div className="live-map-area">
          <NodeOwnerControl
            user={user}
            ownedCount={ownedNodeIds.length}
            ownerOnly={ownerOnly}
            loading={authLoading}
            onToggle={(on) => {
              setOwnerOnly(on);
              // Refit to the user's nodes when entering owner mode.
              if (on) setFocusNonce((n) => n + 1);
            }}
          />
          {showFilters && (
            <div style={{
              position: "absolute", top: 12, left: 52, zIndex: 1000,
              background: "rgba(2,6,23,0.9)", color: "#e2e8f0", border: "1px solid #1e293b",
              borderRadius: 8, padding: "10px 12px", fontSize: 12, display: "flex",
              flexDirection: "column", gap: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
            }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <strong>Filters</strong>
                <button
                  onClick={() => setFilters({ minFl: "", maxFl: "", minGs: "", type: "all" })}
                  style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", fontSize: 11 }}
                >clear</button>
              </div>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                FL
                <input type="number" placeholder="min" value={filters.minFl} style={{ width: 56 }}
                  onChange={(e) => setFilters((f) => ({ ...f, minFl: e.target.value }))} />
                –
                <input type="number" placeholder="max" value={filters.maxFl} style={{ width: 56 }}
                  onChange={(e) => setFilters((f) => ({ ...f, maxFl: e.target.value }))} />
              </label>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                Min speed (kt)
                <input type="number" placeholder="0" value={filters.minGs} style={{ width: 56 }}
                  onChange={(e) => setFilters((f) => ({ ...f, minGs: e.target.value }))} />
              </label>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                Type
                <select value={filters.type} onChange={(e) => setFilters((f) => ({ ...f, type: e.target.value }))}>
                  <option value="all">All</option>
                  <option value="aircraft">Aircraft</option>
                  <option value="drone">Drones</option>
                  <option value="multinode">Multi-node only</option>
                </select>
              </label>
            </div>
          )}
          <MapContainer
            center={[initialHash.lat ?? 34.85, initialHash.lon ?? -82.39]}
            zoom={initialHash.z ?? 9}
            style={{ height: "100%", width: "100%" }}
          >
            <BasemapLayer
              key={tileTheme}
              url={
                tileTheme === "positron"
                  ? "https://{s}.basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}{r}.png"
                  : tileTheme === "osm"
                    ? "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                    : "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
              }
            />

            <ViewportTracker onChange={handleViewportChange} />
            <HashSync
              onMove={handleMapMove}
              showRangeRings={showRangeRings}
              selectedHex={selectedHex}
              smoothRef={smoothRef}
            />
            {userLoc && (
              <CircleMarker
                center={[userLoc.lat, userLoc.lon]}
                radius={7}
                pathOptions={{ color: "#38bdf8", fillColor: "#38bdf8", fillOpacity: 0.7, weight: 2 }}
                interactive={false}
              />
            )}
            <MapClickClear onClear={handleMapClick} />
            <FitBounds aircraft={radarAircraft} nodes={nodes} selectedHex={selectedHex} focusNonce={focusNonce} />
            <FollowController followSelected={followSelected} selectedHex={selectedHex} smoothRef={smoothRef} onDisengage={() => setFollowSelected(false)} />

            {/* Coverage zones — memoized, only re-renders on nodes/showCoverage change */}
            <CoverageLayer visibleNodes={visibleNodes} showCoverage={showCoverage} />

            {/* Node markers — uses full `nodes` list (not viewport-culled) so it only
                re-renders every 30s when node data refreshes, not on every pan/zoom.
                SVG circles all share one composited layer — no per-element pan cost. */}
            <NodeMarkersLayer visibleNodes={nodes} onSelectNode={handleSelectNode} />

            {/* Illuminators (TX towers our nodes use) — off by default, deduped per transmitter */}
            <IlluminatorsLayer visibleNodes={nodes} showIlluminators={showIlluminators} />

            {/* Selected node: detection cone + TX tower + aircraft highlights */}
            {selectedNodeId && (() => {
              const sn = visibleNodes.find((n) => n.node_id === selectedNodeId) || nodes.find((n) => n.node_id === selectedNodeId);
              if (!sn) return null;
              const hasEmpirical = Array.isArray(sn.empirical_polygon) && sn.empirical_polygon.length >= 3;
              const conePositions = yagiSectorPositions(
                sn.rx_lat, sn.rx_lon,
                sn.tx_lat, sn.tx_lon,
                sn.beam_azimuth_deg,
                sn.beam_width_deg ?? 42,
                sn.max_range_km ?? 50,
                sn.max_bistatic_range_km,
              );
              // Find aircraft detected by this node (those whose node_id matches)
              const nodeAircraft = radarAircraft.filter((ac) => ac.node_id === selectedNodeId);
              return (
                <>
                  {/* Empirical detection area — shown when calibration data is available (green solid) */}
                  {hasEmpirical && (
                    <Polygon
                      positions={sn.empirical_polygon}
                      pathOptions={{ color: "#22c55e", fillColor: "#22c55e", fillOpacity: 0.22, weight: 2 }}
                      interactive={false}
                    />
                  )}
                  {/* Theoretical Yagi cone — faint reference behind empirical; full highlight when no empirical data */}
                  <Polygon
                    positions={conePositions}
                    pathOptions={{
                      color: "#fbbf24",
                      fillColor: "#fbbf24",
                      fillOpacity: hasEmpirical ? 0.04 : 0.15,
                      weight: hasEmpirical ? 1 : 2,
                      dashArray: "6 3",
                    }}
                    interactive={false}
                  />
                  {/* TX tower marker */}
                  {sn.tx_lat && sn.tx_lon && (
                    <CircleMarker
                      center={[sn.tx_lat, sn.tx_lon]}
                      radius={8}
                      pathOptions={{ color: "#f59e0b", weight: 2.5, fillColor: "#fbbf24", fillOpacity: 0.7 }}
                      bubblingMouseEvents={false}
                    >
                      <Popup><strong>TX Tower</strong><br />{sn.tx_lat.toFixed(4)}, {sn.tx_lon.toFixed(4)}</Popup>
                    </CircleMarker>
                  )}
                  {/* RX→TX baseline */}
                  <Polyline
                    positions={[[sn.rx_lat, sn.rx_lon], [sn.tx_lat, sn.tx_lon]]}
                    pathOptions={{ color: "#f59e0b", weight: 1.5, opacity: 0.6, dashArray: "4 6" }}
                    interactive={false}
                  />
                  {/* Highlight arcs/markers for aircraft detected by this node */}
                  {nodeAircraft.map((ac) => {
                    if (Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2) {
                      return (
                        <Polyline
                          key={`node-det-${ac.hex}`}
                          positions={ac.ambiguity_arc}
                          pathOptions={{ color: "#fbbf24", weight: 5, opacity: 0.55, lineCap: "round" }}
                          interactive={false}
                        />
                      );
                    }
                    if (ac.lat && ac.lon) {
                      return (
                        <CircleMarker
                          key={`node-det-${ac.hex}`}
                          center={[ac.lat, ac.lon]}
                          radius={12}
                          pathOptions={{ color: "#fbbf24", weight: 2, fillOpacity: 0, dashArray: "4 4" }}
                          interactive={false}
                        />
                      );
                    }
                    return null;
                  })}
                </>
              );
            })()}

            {/* Contributing node highlights — shown when a multinode-solved aircraft is selected */}
            {selectedAc?.multinode && Array.isArray(selectedAc.contributing_node_ids) &&
              selectedAc.contributing_node_ids.map((nid) => {
                const cn = nodes.find((n) => n.node_id === nid);
                if (!cn) return null;
                const hasEmpirical = Array.isArray(cn.empirical_polygon) && cn.empirical_polygon.length >= 3;
                return (
                  <React.Fragment key={`contrib-group-${nid}`}>
                    {/* Coverage area — empirical polygon or Yagi sector */}
                    {hasEmpirical ? (
                      <Polygon
                        positions={cn.empirical_polygon}
                        pathOptions={{ color: "#a78bfa", fillColor: "#a78bfa", fillOpacity: 0.10, weight: 1.5 }}
                        interactive={false}
                      />
                    ) : (
                      <Polygon
                        positions={yagiSectorPositions(
                          cn.rx_lat, cn.rx_lon,
                          cn.tx_lat, cn.tx_lon,
                          cn.beam_azimuth_deg,
                          cn.beam_width_deg ?? 40,
                          cn.max_range_km ?? 50,
                          cn.max_bistatic_range_km,
                        )}
                        pathOptions={{ color: "#a78bfa", fillColor: "#a78bfa", fillOpacity: 0.08, weight: 1.5, dashArray: "5 3" }}
                        interactive={false}
                      />
                    )}
                    {/* Prominent node marker ring */}
                    <CircleMarker
                      center={[cn.rx_lat, cn.rx_lon]}
                      radius={14}
                      pathOptions={{ color: "#a78bfa", weight: 3, fillColor: "#a78bfa", fillOpacity: 0.25 }}
                      interactive={false}
                    />
                    {/* Connection line from aircraft to contributing node */}
                    {selectedAc.lat && selectedAc.lon && (
                      <Polyline
                        positions={[[selectedAc.lat, selectedAc.lon], [cn.rx_lat, cn.rx_lon]]}
                        pathOptions={{ color: "#a78bfa", weight: 1.5, opacity: 0.5, dashArray: "6 4" }}
                        interactive={false}
                      />
                    )}
                  </React.Fragment>
                );
              })
            }

            {/* GT debug — when a simulated (ground-truth) object is selected,
                 ring every node currently detecting it and link them to the
                 object.  Mirrors the multinode contributing-node treatment;
                 amber to match the single-node detection highlight. */}
            {selectedAc?._isTruth && validLatLon(selectedAc.lat, selectedAc.lon) &&
              selectedTruthDetectingNodes.map((nid) => {
                const dn = nodes.find((n) => n.node_id === nid);
                if (!dn) return null;
                return (
                  <React.Fragment key={`gt-det-${nid}`}>
                    <CircleMarker
                      center={[dn.rx_lat, dn.rx_lon]}
                      radius={14}
                      pathOptions={{ color: "#fbbf24", weight: 3, fillColor: "#fbbf24", fillOpacity: 0.25 }}
                      interactive={false}
                    />
                    <Polyline
                      positions={[[selectedAc.lat, selectedAc.lon], [dn.rx_lat, dn.rx_lon]]}
                      pathOptions={{ color: "#fbbf24", weight: 1.5, opacity: 0.6, dashArray: "6 4" }}
                      interactive={false}
                    />
                  </React.Fragment>
                );
              })
            }

            {/* Single-node selection — highlight the source node + connect to
                 aircraft.  Mirrors the multinode block above but for the
                 90 % of tracks that come from a single radar node. */}
            {selectedAc && !selectedAc.multinode && selectedAc.node_id && (() => {
              const sn = nodes.find((n) => n.node_id === selectedAc.node_id);
              if (!sn) return null;
              return (
                <>
                  <CircleMarker
                    center={[sn.rx_lat, sn.rx_lon]}
                    radius={14}
                    pathOptions={{ color: "#fbbf24", weight: 3, fillColor: "#fbbf24", fillOpacity: 0.25 }}
                    interactive={false}
                  />
                  {selectedAc.lat && selectedAc.lon && (
                    <Polyline
                      positions={[[selectedAc.lat, selectedAc.lon], [sn.rx_lat, sn.rx_lon]]}
                      pathOptions={{ color: "#fbbf24", weight: 1.5, opacity: 0.6, dashArray: "6 4" }}
                      interactive={false}
                    />
                  )}
                </>
              );
            })()}

            {/* Per-aircraft trails for every visible target — imperative canvas
                 layer that subscribes to frontendTrailsRef.  Excludes the
                 selected aircraft, which gets the prominent gradient trail
                 rendered below from the same buffer source. */}
            {showTrails && (
              <AircraftTrailsLayer
                visibleAircraftRef={visibleAircraftRef}
                frontendTrailsRef={frontendTrailsRef}
                selectedHex={selectedHex}
              />
            )}

            {/* Selected trail — gradient fade; dashed for arc-type tracks */}
            {showTrails && selectedTrailPositions.length >= 2 && (() => {
              const isArcTrack = selectedAc?.position_source === POSITION_SOURCE_ARC_ONLY;
              return buildTrailSegments(selectedTrailPositions).map((seg, i) => (
                <Polyline
                  key={`trail-${selectedHex}-seg${i}`}
                  positions={seg.positions}
                  pathOptions={{
                    color: "#f59e0b",
                    weight: seg.weight,
                    opacity: isArcTrack ? seg.opacity * 0.6 : seg.opacity,
                    lineCap: "round",
                    lineJoin: "round",
                    dashArray: isArcTrack ? "5 7" : undefined,
                  }}
                  interactive={false}
                />
              ));
            })()}

            {/* Detection arcs — imperative Leaflet layer, 4Hz opacity fade, sourced from raw WS buffer */}
            <DetectionArcs arcsBufferRef={arcsBufferRef} selectedHex={selectedHex} onSelect={handleSelectAircraft} onSelectNode={handleSelectNode} nodesByIdRef={nodesByIdRef} />
            {/* In-beam-no-detection diagnostic — red dashed lines from a node's RX to any
                 ADS-B aircraft sitting inside its beam that the node is NOT currently detecting. */}
            {showInBeamDiag && (
              <InBeamDiagnostic detectionsRef={detectionsRef} groundTruthRef={groundTruthRef} nodesByIdRef={nodesByIdRef} smoothRef={smoothRef} />
            )}
            {/* Aircraft position markers — radar-detected aircraft rendered as airplane icons.
                 Color encodes confidence: purple=multinode, teal=ADS-B aided, cyan=single-node.
                 Single-node arc-only tracks get NO plane marker: their lat/lon is just the
                 arc-midpoint estimate (the aircraft is somewhere along the visible arc), and
                 users mistook the icon position for the actual location.  The detection arc
                 rendered by DetectionArcs is their only map presence; selecting them from the
                 list still highlights the arc and centers the map on the midpoint. */}
            {visibleAircraft.map((ac) => {
              if (!validLatLon(ac.lat, ac.lon)) return null;
              if (ac.position_source === POSITION_SOURCE_ARC_ONLY) return null;
              const isSelected = ac.hex === selectedHex;
              return (
                <AircraftMarker
                  key={`icon-${ac.hex}`}
                  ac={ac}
                  isSelected={isSelected}
                  showLabels={showLabels}
                  colorByAlt={colorByAlt}
                  onSelect={handleSelectAircraft}
                  markerRegistry={markerRegistryRef.current}
                />
              );
            })}

            {/* Anomaly flag rings — pulsing red circle around flagged aircraft */}
            {visibleAircraft
              .filter((ac) =>
                anomalyHexesRef.current.has(ac.ground_truth_hex || ac.hex) &&
                ac.lat && ac.lon
              )
              .map((ac) => (
                <CircleMarker
                  key={`anomaly-${ac.hex}`}
                  center={[ac.lat, ac.lon]}
                  radius={16}
                  pathOptions={{
                    color: "#f43f5e",
                    weight: 2.5,
                    fillOpacity: 0,
                    dashArray: "5 5",
                    className: "anomaly-ring",
                  }}
                  interactive={false}
                />
              ))}

            {/* Ground-truth-only markers — single canvas layer, O(1) DOM regardless of count */}
            {showGroundTruth && (
              <GroundTruthCanvasLayer
                aircraft={visibleTruthOnlyAircraft}
                onSelect={handleSelectAircraft}
                selectedHex={selectedHex}
              />
            )}

            {/* Matched GT overlay — shows GT dots + error lines for radar aircraft with GT match */}
            {showGroundTruth && (
              <MatchedGroundTruthLayer
                radarAircraftRef={radarAircraftRef}
                groundTruthRef={groundTruthRef}
                smoothRef={smoothRef}
                nodesByIdRef={nodesByIdRef}
              />
            )}

            {/* Radar3 solver verification overlay — truth dots + error lines + km labels */}
            <NodeVerificationLayer nodeId={verificationNodeId} />

            {/* MLAT (multinode) solver verification — magenta truth dots + pink error lines */}
            {/* Gated like the Radar3 layers: this polls /api/test/
                mlat-verification and draws truth-vs-solver error lines, which
                is meaningless (and a wasted poll) when ground truth is off —
                as it is by default on the production map domains. */}
            {showGroundTruth && (
              <MlatVerificationLayer
                groundTruthRef={groundTruthRef}
                smoothRef={smoothRef}
              />
            )}

            {/* Raw solve positions behind the selected MLAT marker */}
            {mlatHistory?.solves?.length > 0 && (
              <MlatSolveHistoryLayer solves={mlatHistory.solves} />
            )}
          </MapContainer>

          <StatsOverlay
            aircraft={radarAircraft}
            truth={showGroundTruth ? truthOnlyAircraft : []}
            anomalyCount={anomalyCount}
            visible={showStats}
            onToggle={() => setShowStats((v) => !v)}
          />
          <ShortcutHelp visible={showShortcutHelp} onClose={() => setShowShortcutHelp(false)} />

          {selectedAc && (
            <AircraftDetailPanel
              ac={selectedAc}
              onClose={() => setSelectedHex(null)}
              groundTruth={groundTruthRef.current}
              trails={trailsRef.current}
              computeError={computeError}
              detectingNodes={selectedTruthDetectingNodes}
              solveHistory={mlatHistory}
            />
          )}

          {paused && historyRef.current.length > 0 && (
            <PlaybackBar
              history={historyRef.current}
              value={seekIndex ?? historyRef.current.length - 1}
              onSeek={handleHistorySeek}
              formatSecondsAgo={formatSecondsAgo}
            />
          )}
        </div>
      </div>
    </div>
  );
}
