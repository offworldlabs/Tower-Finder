// @ts-nocheck — gradual TS migration; will type incrementally
import React, { useEffect, useRef, useState, useCallback, useMemo, memo } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  CircleMarker,
  Polygon,
  Polyline,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import "./LiveAircraftMap.css";

import {
  STALE_AIRCRAFT_MS,
  POSITION_SOURCE_ARC_ONLY,
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
  AircraftListPanel,
  AircraftDetailPanel,
  Toolbar,
  PlaybackBar,
  DetectionArcs,
} from "./map";

import { fetchRadar3Verification, fetchRadar3DetectionRange, fetchMlatVerification } from "../api";
import { defaultsGroundTruthOff } from "../utils/domains";

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
      WS update (~1Hz). L.canvas() draws everything in one canvas tile — O(1) DOM. ── */
const _gtCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5 }) : null;

const GroundTruthCanvasLayer = memo(function GroundTruthCanvasLayer({ aircraft, onSelect }) {
  const map = useMap();
  const markerMapRef = useRef(new Map()); // hex → L.circleMarker — incremental diff
  const onSelectRef  = useRef(onSelect);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);

  useEffect(() => {
    const markerMap = markerMapRef.current;
    const seen = new Set();

    for (const ac of aircraft) {
      seen.add(ac.hex);
      const isAnom  = ac.is_anomalous;
      const isDrone = ac.object_type === "drone";
      const color   = isAnom ? "#f43f5e" : isDrone ? "#f59e0b" : "#22d3ee";
      const border  = isAnom ? "#e11d48" : isDrone ? "#d97706" : "#67e8f9";
      const radius  = isDrone ? 6 : isAnom ? 8 : 9;

      let m = markerMap.get(ac.hex);
      if (!m) {
        m = L.circleMarker([ac.lat, ac.lon], {
          renderer: _gtCanvas,
          radius,
          color: border,
          weight: 3,
          fillColor: color,
          fillOpacity: 0.7,
        });
        m.on("click", () => onSelectRef.current(ac.hex));
        m.addTo(map);
        markerMap.set(ac.hex, m);
      } else {
        m.setLatLng([ac.lat, ac.lon]);
        m.setStyle({ color: border, fillColor: color });
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
  }, [aircraft, map]);

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
      single canvas.  Updated at 4Hz from smoothRef for dead-reckoned positions. ── */
const _mgCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5 }) : null;

const MatchedGroundTruthLayer = memo(function MatchedGroundTruthLayer({ radarAircraft, groundTruthRef, smoothRef }) {
  const map = useMap();
  const markersRef = useRef(new Map());  // gtHex → { dot: L.circleMarker, line: L.polyline }

  useEffect(() => {
    const markers = markersRef.current;

    const tick = () => {
      const gt = groundTruthRef.current;
      const seen = new Set();

      for (const ac of radarAircraft) {
        const gtHex = ac.ground_truth_hex;
        if (!gtHex) continue;
        const gtTrail = gt[gtHex];
        if (!Array.isArray(gtTrail) || gtTrail.length === 0) continue;

        // GT position from smoothRef (dead-reckoned at 60fps) if available, else raw
        const smooth = smoothRef.current[gtHex];
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
        const rLat = radarSmooth ? radarSmooth.lat : ac.lat;
        const rLon = radarSmooth ? radarSmooth.lon : ac.lon;

        if (!gtLat || !gtLon || !rLat || !rLon) continue;

        seen.add(gtHex);
        let entry = markers.get(gtHex);
        if (!entry) {
          const dot = L.circleMarker([gtLat, gtLon], {
            renderer: _mgCanvas,
            radius: 5,
            color: "#22d3ee",
            weight: 2,
            fillColor: "#22d3ee",
            fillOpacity: 0.8,
          });
          const line = L.polyline([[gtLat, gtLon], [rLat, rLon]], {
            color: "#facc15",
            weight: 1.5,
            opacity: 0.6,
            dashArray: "3 4",
          });
          dot.addTo(map);
          line.addTo(map);
          entry = { dot, line };
          markers.set(gtHex, entry);
        } else {
          entry.dot.setLatLng([gtLat, gtLon]);
          entry.line.setLatLngs([[gtLat, gtLon], [rLat, rLon]]);
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
  }, [map, radarAircraft, groundTruthRef, smoothRef]);

  return null;
});

/* ── Radar3VerificationLayer: shows solver tracks vs ADS-B truth for radar3 node.
      Fetches verification data every 15s and renders cyan truth dots,
      yellow error lines, and distance labels. ── */
const _r3Canvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5 }) : null;

const Radar3VerificationLayer = memo(function Radar3VerificationLayer({ visible }) {
  const map = useMap();
  const markersRef = useRef(new Map());
  const dataRef = useRef(null);

  useEffect(() => {
    if (!visible) {
      // Clean up when hidden
      for (const entry of markersRef.current.values()) {
        entry.dot.remove();
        entry.line.remove();
        if (entry.label) entry.label.remove();
      }
      markersRef.current.clear();
      return;
    }

    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await fetchRadar3Verification();
        if (cancelled || !data) return;
        dataRef.current = data;

        const markers = markersRef.current;
        const seen = new Set();

        for (const t of data.tracks || []) {
          if (!t.truth_lat || !t.truth_lon || !t.solver_lat || !t.solver_lon) continue;
          seen.add(t.hex);

          let entry = markers.get(t.hex);
          if (!entry) {
            const dot = L.circleMarker([t.truth_lat, t.truth_lon], {
              renderer: _r3Canvas,
              radius: 5,
              color: "#22d3ee",
              weight: 2,
              fillColor: "#22d3ee",
              fillOpacity: 0.8,
            });
            const line = L.polyline(
              [[t.truth_lat, t.truth_lon], [t.solver_lat, t.solver_lon]],
              { color: "#facc15", weight: 1.5, opacity: 0.7, dashArray: "3 4" },
            );
            line.bindTooltip(`${t.position_error_km.toFixed(1)} km`, { direction: "center", className: "radar3-error-label" });
            dot.addTo(map);
            line.addTo(map);
            entry = { dot, line };
            markers.set(t.hex, entry);
          } else {
            entry.dot.setLatLng([t.truth_lat, t.truth_lon]);
            entry.line.setLatLngs([[t.truth_lat, t.truth_lon], [t.solver_lat, t.solver_lon]]);
            entry.line.setTooltipContent(`${t.position_error_km.toFixed(1)} km`);
          }
        }

        for (const [hex, entry] of markers) {
          if (!seen.has(hex)) {
            entry.dot.remove();
            entry.line.remove();
            markers.delete(hex);
          }
        }
      } catch (e) {
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
  }, [map, visible]);

  return null;
});

/* ── Radar3RangeLayer: dashed range circle + furthest detection markers ── */
const Radar3RangeLayer = memo(function Radar3RangeLayer({ visible }) {
  const map = useMap();
  const layersRef = useRef([]);
  const dataRef = useRef(null);

  useEffect(() => {
    // Clean up previous layers
    for (const l of layersRef.current) l.remove();
    layersRef.current = [];

    if (!visible) return;

    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await fetchRadar3DetectionRange();
        if (cancelled || !data || data.error) return;
        // Clean previous
        for (const l of layersRef.current) l.remove();
        layersRef.current = [];
        dataRef.current = data;

        const rx = data.rx;
        if (!rx) return;

        // Max range circle (dashed)
        const rangeKm = data.estimated_max_range_km;
        if (rangeKm > 0) {
          const circle = L.circle([rx.lat, rx.lon], {
            radius: rangeKm * 1000,
            color: "#f97316",
            weight: 2,
            fillOpacity: 0,
            dashArray: "8 6",
          });
          circle.bindTooltip(`Max range: ${rangeKm.toFixed(1)} km`, { direction: "top" });
          circle.addTo(map);
          layersRef.current.push(circle);
        }

        // Furthest detections markers
        for (const det of data.furthest_detections || []) {
          const marker = L.circleMarker([det.lat, det.lon], {
            radius: 7,
            color: "#f97316",
            weight: 2,
            fillColor: "#f97316",
            fillOpacity: 0.6,
          });
          marker.bindTooltip(
            `${det.distance_km.toFixed(1)} km${det.hex ? ` (${det.hex})` : ""}`,
            { direction: "top" },
          );
          marker.addTo(map);
          layersRef.current.push(marker);
        }
      } catch (e) {
        // Silently ignore
      }
    };

    refresh();
    return () => {
      cancelled = true;
      for (const l of layersRef.current) l.remove();
      layersRef.current = [];
    };
  }, [map, visible]);

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
const _mlatCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5 }) : null;

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
    const markers = markersRef.current;

    const tick = () => {
      const seen = new Set();
      const tracks = tracksRef.current;

      for (const t of tracks) {
        if (!t.truth_lat || !t.truth_lon || !t.solver_lat || !t.solver_lon) continue;
        const hex = t.truth_hex;
        if (!hex) continue;

        // Live truth position: prefer the smoothed value, fall back to the
        // most recent raw trail point. Skip if we have neither — the
        // verification payload alone isn't enough to render aligned.
        let liveLat;
        let liveLon;
        const smooth = smoothRef?.current?.[hex];
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
            radius: 4,
            color: "#e879f9",
            weight: 2,
            fillColor: "#e879f9",
            fillOpacity: 0.85,
          });
          const line = L.polyline(
            [[drTruthLat, drTruthLon], [drSolverLat, drSolverLon]],
            { color: "#f0abfc", weight: 1.5, opacity: 0.7, dashArray: "3 4" },
          );
          line.bindTooltip(
            `${t.position_error_km.toFixed(1)} km`,
            { direction: "center", className: "radar3-error-label" },
          );
          dot.addTo(map);
          line.addTo(map);
          entry = { dot, line };
          markers.set(id, entry);
        } else {
          entry.dot.setLatLng([drTruthLat, drTruthLon]);
          entry.line.setLatLngs([[drTruthLat, drTruthLon], [drSolverLat, drSolverLon]]);
          entry.line.setTooltipContent(`${t.position_error_km.toFixed(1)} km`);
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

/* ── AircraftMarker: memoized with custom comparator — only re-renders on visual changes
      (selection, labels, callsign, altitude band, type).  lat/lon/track/gs are updated
      imperatively at 60fps via markerRegistry → marker.setLatLng() in the RAF loop,
      completely bypassing React reconcile. ── */
const AircraftMarker = memo(function AircraftMarker({ ac, isSelected, showLabels, onSelect, markerRegistry }) {
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
      : makeAircraftIcon(ac, showLabels, isSelected),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ac.hex, isSelected, showLabels, ac.flight, ac.target_class, altBand],
  );
  const handlers = useMemo(() => ({ click: () => onSelect(ac.hex) }), [ac.hex, onSelect]);
  return <Marker ref={markerRef} position={[ac.lat, ac.lon]} icon={icon} eventHandlers={handlers} />;
}, (prev, next) =>
  // Skip re-render when ONLY position/velocity changed — those are patched live
  // by the RAF loop via marker.setLatLng() without touching React at all.
  prev.isSelected === next.isSelected &&
  prev.showLabels === next.showLabels &&
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
const _trailsCanvas = typeof window !== "undefined" ? L.canvas({ padding: 0.5 }) : null;

const AircraftTrailsLayer = memo(function AircraftTrailsLayer({ visibleAircraft, frontendTrailsRef, selectedHex }) {
  const map = useMap();
  const linesRef = useRef(new Map()); // hex → L.polyline

  useEffect(() => {
    const lines = linesRef.current;
    const tick = () => {
      const trails = frontendTrailsRef.current || {};
      const seen = new Set();
      for (const ac of visibleAircraft) {
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
  }, [map, visibleAircraft, frontendTrailsRef, selectedHex]);

  return null;
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
          eventHandlers={{ click: () => onSelectNode(n.node_id) }}
        >
          <Popup>
            <strong>{n.node_id}</strong><br />
            Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
            Range: {n.max_range_km} km
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
          Range: {n.max_range_km} km
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
        )}
        pathOptions={{ color: "#facc15", fillColor: "#facc15", fillOpacity: 0.1, weight: 1.5, dashArray: "4 4" }}
      />
    );
  });
});

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
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
  } = useAircraftFeed();

  const nodes = useNodes();
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
  const [displayAircraft, setDisplayAircraft] = useState([]);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showTrails, setShowTrails] = useState(true);
  // Default GT on for testmap/staging-testmap (simulation demo); off on map.* and staging-map.* (real only)
  const [showGroundTruth, setShowGroundTruth] = useState(() => !defaultsGroundTruthOff);
  const [showLabels, setShowLabels] = useState(true);
  const [selectedHex, setSelectedHex] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [viewport, setViewport] = useState(null);
  const [showAnomaliesOnly, setShowAnomaliesOnly] = useState(false);

  const animationFrameRef = useRef(null);
  const displayedAircraftRef = useRef({});
  const fixesRef = useRef({});   // hex → last server fix
  const smoothRef = useRef({});  // hex → { lat, lon, track } — smoothed render position
  const prevTsRef = useRef(null);
  const svgElemsRef = useRef({}); // hex → cached SVG DOM element (avoids querySelector every frame)
  const rafFrameRef = useRef(0);  // throttle React re-renders to ~2fps (position/rotation at 60fps via direct L.Marker/DOM)
  const markerRegistryRef = useRef(new Map()); // hex → L.Marker for imperative 60fps setLatLng
  const latLngCacheRef    = useRef({});         // hex → L.LatLng — mutated in place to avoid per-frame allocation
  // Per-hex trail of smoothed lat/lon samples taken from the 60fps DR loop.
  // For arc-only tracks the backend's recent_positions stays at 1 point
  // (append_track_history dedupes positions < 5 m apart, and the arc midpoint
  // doesn't change between detections).  Sampling smoothRef into a frontend
  // buffer at ~2 Hz lets us draw a trail behind the dead-reckoned icon
  // without needing backend changes.  Bounded to 60 samples per hex (30 s).
  const frontendTrailsRef = useRef({});  // hex → Array<[lat, lon, ts_sec]>
  const lastTrailSampleRef = useRef({}); // hex → last sample timestamp (ms)

  /* ── Record server fixes when new WS data arrives ───────────── */
  useEffect(() => {
    const now = Date.now();
    for (const ac of aircraft) {
      if (!ac.lat || !ac.lon) continue;
      const prev = fixesRef.current[ac.hex];
      const posChanged = !prev || prev._fixLat !== ac.lat || prev._fixLon !== ac.lon;
      fixesRef.current[ac.hex] = {
        ...ac,
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
    for (const hex of Object.keys(fixesRef.current)) {
      if (fixesRef.current[hex]._isTruth) continue;
      if (now - (fixesRef.current[hex]._updatedAt ?? 0) > STALE_AIRCRAFT_MS) {
        delete fixesRef.current[hex];
        delete smoothRef.current[hex];
        delete svgElemsRef.current[hex];
        delete frontendTrailsRef.current[hex];
        delete lastTrailSampleRef.current[hex];
      }
    }
  }, [aircraft]);

  /* ── Continuous 60fps glide loop (dead-reckoning + exponential smoothing) ── */
  useEffect(() => {
    const DEG_PER_M = 1 / 111_320;
    const KNOTS_TO_MS = 0.514444;
    // Smoothing time constant: lower = snappier, higher = more glide (seconds)
    const TAU = 0.55;

    const tick = (ts) => {
      const dt = prevTsRef.current !== null ? Math.min((ts - prevTsRef.current) / 1000, 0.1) : 0;
      prevTsRef.current = ts;
      const alpha = dt > 0 ? 1 - Math.exp(-dt / TAU) : 1;

      const now = Date.now();
      const fixes = fixesRef.current;
      for (const fix of Object.values(fixes)) {
        const elapsed = Math.min((now - fix._fixTs) / 1000, 60);
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
        const prev = smoothRef.current[fix.hex];
        const sLat = prev ? prev.lat + (targetLat - prev.lat) * alpha : targetLat;
        const sLon = prev ? prev.lon + (targetLon - prev.lon) * alpha : targetLon;

        // Smooth heading with wrap-around handling
        const targetTrack = fix.track || 0;
        const prevTrack = prev ? prev.track : targetTrack;
        const dTrack = ((targetTrack - prevTrack + 540) % 360) - 180;
        const sTrack = (prevTrack + dTrack * alpha + 360) % 360;

        // Mutate smooth entry in place — avoids 412 short-lived object creations per frame
        const sm = smoothRef.current[fix.hex];
        if (sm) { sm.lat = sLat; sm.lon = sLon; sm.track = sTrack; }
        else     smoothRef.current[fix.hex] = { lat: sLat, lon: sLon, track: sTrack };

        // Update rotation directly on the DOM — avoids setIcon() every frame
        // Cache element reference to avoid querySelector on every 16ms frame
        let svgEl = svgElemsRef.current[fix.hex];
        if (!svgEl || !svgEl.isConnected) {
          svgEl = document.querySelector(`.ac-hex-${CSS.escape(fix.hex)} svg`);
          if (svgEl) svgElemsRef.current[fix.hex] = svgEl;
          else delete svgElemsRef.current[fix.hex];
        }
        if (svgEl) svgEl.style.transform = `rotate(${sTrack.toFixed(1)}deg)`;

        // Imperative Leaflet position — reuse cached L.LatLng and call marker.update() directly
        // to avoid per-frame LatLng + event-object allocations (was ~25k allocs/s at 60fps×412).
        const marker = markerRegistryRef.current.get(fix.hex);
        if (marker) {
          let ll = latLngCacheRef.current[fix.hex];
          if (ll) { ll.lat = sLat; ll.lng = sLon; }
          else { ll = L.latLng(sLat, sLon); latLngCacheRef.current[fix.hex] = ll; }
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
        const lastSample = lastTrailSampleRef.current[fix.hex] || 0;
        if (now - lastSample >= 500) {
          lastTrailSampleRef.current[fix.hex] = now;
          let trail = frontendTrailsRef.current[fix.hex];
          if (!trail) { trail = []; frontendTrailsRef.current[fix.hex] = trail; }
          trail.push([sLat, sLon, now / 1000]);
          if (trail.length > 60) trail.shift();
        }
      }

      // Build React display array at 2fps only — avoids ~25k spread-object allocations/s at 60fps.
      rafFrameRef.current = (rafFrameRef.current + 1) % 30;
      if (rafFrameRef.current === 0) {
        const arr = [];
        const dispMap = {};
        for (const fix of Object.values(fixes)) {
          const sm = smoothRef.current[fix.hex];
          if (!sm) continue;
          const item = { ...fix, lat: sm.lat, lon: sm.lon, track: sm.track };
          arr.push(item);
          dispMap[fix.hex] = item;
        }
        displayedAircraftRef.current = dispMap;
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
      const base = displayAircraft.filter((ac) => ac.position_source || ac.multinode);
      if (showAnomaliesOnly) return base.filter((ac) => ac.is_anomalous);
      return base;
    },
    [displayAircraft, showAnomaliesOnly],
  );

  const anomalyCount = useMemo(
    () => displayAircraft.filter((ac) => (ac.position_source || ac.multinode) && ac.is_anomalous).length,
    [displayAircraft],
  );

  /* ── Derived: truth-only aircraft ───────────────────────────── */
  const matchedTruthHexes = useMemo(
    () => new Set(radarAircraft.map((ac) => ac.ground_truth_hex || ac.hex).filter(Boolean)),
    [radarAircraft],
  );

  /* ── Feed ground-truth objects into fixesRef so the 60fps loop dead-reckons them ── */
  useEffect(() => {
    const now = Date.now();
    const activeGtHexes = new Set();
    for (const [hex, positions] of Object.entries(groundTruthRef.current)) {
      if (!Array.isArray(positions) || positions.length === 0) continue;
      const last = positions[positions.length - 1];
      const meta = groundTruthMetaRef.current[hex] || {};
      const lat = last[0], lon = last[1];
      activeGtHexes.add(hex);
      const prev = fixesRef.current[hex];
      const posChanged = !prev || prev._fixLat !== lat || prev._fixLon !== lon;
      fixesRef.current[hex] = {
        hex,
        lat, lon,
        alt_baro: Math.round(last[2] / 0.3048),
        gs: Math.round((meta.speed_ms || 0) * 1.94384 * 10) / 10,
        track: meta.heading || 0,
        object_type: meta.object_type,
        is_anomalous: meta.is_anomalous,
        points: positions.length,
        _isTruth: true,
        _fixLat: lat,
        _fixLon: lon,
        _fixTs: posChanged ? now : (prev?._fixTs ?? now),
        _updatedAt: now,
      };
    }
    // Remove ground-truth entries that are no longer in the server snapshot
    for (const hex of Object.keys(fixesRef.current)) {
      if (!fixesRef.current[hex]._isTruth) continue;
      if (!activeGtHexes.has(hex)) {
        delete fixesRef.current[hex];
        delete smoothRef.current[hex];
      }
    }
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
    const frontTrail = frontendTrailsRef.current[selectedHex];
    if (frontTrail && frontTrail.length) {
      const lastBack = pts[pts.length - 1];
      for (const [lat, lon] of frontTrail) {
        if (lastBack && Math.abs(lastBack[0] - lat) < 1e-5 && Math.abs(lastBack[1] - lon) < 1e-5) continue;
        pts.push([lat, lon]);
      }
    }
    // smoothRef is updated at 60fps (vs displayedAircraftRef which is only 2fps)
    // so the trail tip connects exactly to the current smoothed position.
    const animated = smoothRef.current[selectedHex];
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
  }

  function handleHistorySeek(index) {
    if (index >= 0 && index < historyRef.current.length) {
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

  function computeError(hex, ac) {
    const gtHex = ac.ground_truth_hex || hex;
    const gtTrail = groundTruthRef.current[gtHex];
    if (!gtTrail || !gtTrail.length) return null;
    const last = gtTrail[gtTrail.length - 1];
    const dlat = (ac.lat - last[0]) * 111.0;
    const dlon = (ac.lon - last[1]) * 111.0 * Math.cos((ac.lat * Math.PI) / 180);
    return Math.sqrt(dlat * dlat + dlon * dlon);
  }

  function formatSecondsAgo(ts) {
    const sec = Math.round((Date.now() - ts) / 1000);
    return sec <= 0 ? "now" : `-${sec}s`;
  }

  /* ── Render ─────────────────────────────────────────────────── */
  return (
    <div className="live-map-container">
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
        onToggleCoverage={() => setShowCoverage((v) => !v)}
        onToggleLabels={() => setShowLabels((v) => !v)}
        onToggleTrails={() => setShowTrails((v) => !v)}
        onToggleGroundTruth={() => setShowGroundTruth((v) => !v)}
        onToggleAnomaliesOnly={() => setShowAnomaliesOnly((v) => !v)}
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
        />

        <div className="live-map-area">
          <MapContainer center={[34.0, -84.5]} zoom={8} style={{ height: "100%", width: "100%" }} attributionControl={false}>
            <TileLayer
              url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
            />

            <ViewportTracker onChange={handleViewportChange} />
            <MapClickClear onClear={handleMapClick} />
            <FitBounds aircraft={radarAircraft} nodes={nodes} selectedHex={selectedHex} focusNonce={focusNonce} />

            {/* Coverage zones — memoized, only re-renders on nodes/showCoverage change */}
            <CoverageLayer visibleNodes={visibleNodes} showCoverage={showCoverage} />

            {/* Node markers — uses full `nodes` list (not viewport-culled) so it only
                re-renders every 30s when node data refreshes, not on every pan/zoom.
                SVG circles all share one composited layer — no per-element pan cost. */}
            <NodeMarkersLayer visibleNodes={nodes} onSelectNode={handleSelectNode} />

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
                  />
                  {/* TX tower marker */}
                  {sn.tx_lat && sn.tx_lon && (
                    <CircleMarker
                      center={[sn.tx_lat, sn.tx_lon]}
                      radius={8}
                      pathOptions={{ color: "#f59e0b", weight: 2.5, fillColor: "#fbbf24", fillOpacity: 0.7 }}
                    >
                      <Popup><strong>TX Tower</strong><br />{sn.tx_lat.toFixed(4)}, {sn.tx_lon.toFixed(4)}</Popup>
                    </CircleMarker>
                  )}
                  {/* RX→TX baseline */}
                  <Polyline
                    positions={[[sn.rx_lat, sn.rx_lon], [sn.tx_lat, sn.tx_lon]]}
                    pathOptions={{ color: "#f59e0b", weight: 1.5, opacity: 0.6, dashArray: "4 6" }}
                  />
                  {/* Highlight arcs/markers for aircraft detected by this node */}
                  {nodeAircraft.map((ac) => {
                    if (Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2) {
                      return (
                        <Polyline
                          key={`node-det-${ac.hex}`}
                          positions={ac.ambiguity_arc}
                          pathOptions={{ color: "#fbbf24", weight: 5, opacity: 0.55, lineCap: "round" }}
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
                      />
                    ) : (
                      <Polygon
                        positions={yagiSectorPositions(
                          cn.rx_lat, cn.rx_lon,
                          cn.tx_lat, cn.tx_lon,
                          cn.beam_azimuth_deg,
                          cn.beam_width_deg ?? 40,
                          cn.max_range_km ?? 50,
                        )}
                        pathOptions={{ color: "#a78bfa", fillColor: "#a78bfa", fillOpacity: 0.08, weight: 1.5, dashArray: "5 3" }}
                      />
                    )}
                    {/* Prominent node marker ring */}
                    <CircleMarker
                      center={[cn.rx_lat, cn.rx_lon]}
                      radius={14}
                      pathOptions={{ color: "#a78bfa", weight: 3, fillColor: "#a78bfa", fillOpacity: 0.25 }}
                    />
                    {/* Connection line from aircraft to contributing node */}
                    {selectedAc.lat && selectedAc.lon && (
                      <Polyline
                        positions={[[selectedAc.lat, selectedAc.lon], [cn.rx_lat, cn.rx_lon]]}
                        pathOptions={{ color: "#a78bfa", weight: 1.5, opacity: 0.5, dashArray: "6 4" }}
                      />
                    )}
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
                  />
                  {selectedAc.lat && selectedAc.lon && (
                    <Polyline
                      positions={[[selectedAc.lat, selectedAc.lon], [sn.rx_lat, sn.rx_lon]]}
                      pathOptions={{ color: "#fbbf24", weight: 1.5, opacity: 0.6, dashArray: "6 4" }}
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
                visibleAircraft={visibleAircraft}
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
                />
              ));
            })()}

            {/* Detection arcs — imperative Leaflet layer, 4Hz opacity fade, sourced from raw WS buffer */}
            <DetectionArcs arcsBufferRef={arcsBufferRef} selectedHex={selectedHex} onSelect={handleSelectAircraft} onSelectNode={handleSelectNode} smoothRef={smoothRef} nodesByIdRef={nodesByIdRef} />
            {/* Aircraft position markers — all radar-detected aircraft rendered as airplane icons.
                 Color encodes confidence: purple=multinode, teal=ADS-B aided, cyan=single-node.
                 Single-node arc-only tracks are rendered smaller / dashed / semi-transparent to
                 communicate that their position is approximate (the aircraft is somewhere along
                 the visible arc, not exactly at the midpoint). */}
            {visibleAircraft.map((ac) => {
              if (!ac.lat || !ac.lon) return null;
              const isSelected = ac.hex === selectedHex;
              return (
                <AircraftMarker
                  key={`icon-${ac.hex}`}
                  ac={ac}
                  isSelected={isSelected}
                  showLabels={showLabels}
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
                />
              ))}

            {/* Ground-truth-only markers — single canvas layer, O(1) DOM regardless of count */}
            {showGroundTruth && (
              <GroundTruthCanvasLayer
                aircraft={visibleTruthOnlyAircraft}
                onSelect={handleSelectAircraft}
              />
            )}

            {/* Matched GT overlay — shows GT dots + error lines for radar aircraft with GT match */}
            {showGroundTruth && (
              <MatchedGroundTruthLayer
                radarAircraft={radarAircraft}
                groundTruthRef={groundTruthRef}
                smoothRef={smoothRef}
              />
            )}

            {/* Radar3 solver verification overlay — truth dots + error lines + km labels */}
            <Radar3VerificationLayer visible={selectedNodeId === "radar3-retnode"} />

            {/* Radar3 detection range circle + furthest detection markers */}
            <Radar3RangeLayer visible={selectedNodeId === "radar3-retnode"} />

            {/* MLAT (multinode) solver verification — magenta truth dots + pink error lines */}
            <MlatVerificationLayer
              groundTruthRef={groundTruthRef}
              smoothRef={smoothRef}
            />
          </MapContainer>

          {selectedAc && (
            <AircraftDetailPanel
              ac={selectedAc}
              onClose={() => setSelectedHex(null)}
              groundTruth={groundTruthRef.current}
              trails={trailsRef.current}
              computeError={computeError}
            />
          )}

          {paused && historyRef.current.length > 0 && (
            <PlaybackBar
              history={historyRef.current}
              onSeek={handleHistorySeek}
              formatSecondsAgo={formatSecondsAgo}
            />
          )}
        </div>
      </div>
    </div>
  );
}
