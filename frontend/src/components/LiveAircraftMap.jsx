import { useEffect, useRef, useState, useCallback, useMemo, memo } from "react";
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
  dopplerColor,
  isPointInViewport,
  isAircraftInViewport,
  sampleTrailPositions,
  buildTrailSegments,
  makeAircraftIcon,
  makeDroneIcon,
  yagiSectorPositions,
  FitBounds,
  ViewportTracker,
  useAircraftFeed,
  useNodes,
  AircraftListPanel,
  AircraftDetailPanel,
  Toolbar,
  PlaybackBar,
} from "./map";

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
      const radius  = isDrone ? 5 : isAnom ? 7 : 6;

      let m = markerMap.get(ac.hex);
      if (!m) {
        m = L.circleMarker([ac.lat, ac.lon], {
          renderer: _gtCanvas,
          radius,
          color: border,
          weight: 2,
          fillColor: color,
          fillOpacity: 0.45,
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

/* ── NodeMarkersLayer: memoized + SVG CircleMarkers (NOT DOM divIcon Markers).
      914 DOM divs with drop-shadow filters caused severe pan/zoom jank.
      SVG circles live in a single overlay — browser composites ONE layer. ── */
const NodeMarkersLayer = memo(function NodeMarkersLayer({ visibleNodes, onSelectNode }) {
  return visibleNodes.map((n) => (
    <CircleMarker
      key={`node-${n.node_id}`}
      center={[n.rx_lat, n.rx_lon]}
      radius={5}
      pathOptions={{ color: "#ef4444", fillColor: "#ef4444", fillOpacity: 0.55, weight: 1.5 }}
      eventHandlers={{ click: () => onSelectNode(n.node_id) }}
    >
      <Popup>
        <strong>{n.node_id}</strong><br />
        Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
        Range: {n.max_range_km} km
      </Popup>
    </CircleMarker>
  ));
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
        pathOptions={{ color: "#ef4444", fillColor: "#ef4444", fillOpacity: 0.1, weight: 1.5, dashArray: "4 4" }}
      />
    );
  });
});

/* ── DetectionArcs: memoized — each arc fades independently based on its last-update timestamp.
      Multiple arcs can exist per aircraft (one per detecting node). ── */
const DetectionArcs = memo(function DetectionArcs({ visibleArcs, selectedHex, onSelect, onSelectNode }) {
  return visibleArcs.map((arc) => {
    const isSelected = arc.hex === selectedHex;
    const arcAge = Date.now() - arc.ts;
    const arcOpacity = Math.max(0.08, Math.min(0.95, 1 - Math.max(0, arcAge - 2000) / 18000));
    const arcColor = arc.target_class === "drone" ? "#fb923c" : dopplerColor(arc.doppler_hz ?? 0);
    return (
      <Polyline
        key={arc._key}
        positions={arc.ambiguity_arc}
        pathOptions={{
          color: arcColor,
          weight: isSelected ? 5 : 3,
          opacity: isSelected ? 1 : arcOpacity,
          lineCap: "round",
          lineJoin: "round",
        }}
        eventHandlers={{ click: () => { onSelect(arc.hex); if (arc.node_id) onSelectNode(arc.node_id); } }}
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

  /* ── Local UI state ─────────────────────────────────────────── */
  const [displayAircraft, setDisplayAircraft] = useState([]);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showTrails, setShowTrails] = useState(true);
  // Default GT on for testmap (simulation demo); off on map.retina.fm (real only)
  const [showGroundTruth, setShowGroundTruth] = useState(
    () => !/^map\./i.test(window.location.hostname),
  );
  const [showLabels, setShowLabels] = useState(true);
  const [selectedHex, setSelectedHex] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [viewport, setViewport] = useState(null);

  const animationFrameRef = useRef(null);
  const displayedAircraftRef = useRef({});
  const fixesRef = useRef({});   // hex → last server fix
  const smoothRef = useRef({});  // hex → { lat, lon, track } — smoothed render position
  const prevTsRef = useRef(null);
  const svgElemsRef = useRef({}); // hex → cached SVG DOM element (avoids querySelector every frame)
  const rafFrameRef = useRef(0);  // throttle React re-renders to ~2fps (position/rotation at 60fps via direct L.Marker/DOM)
  const markerRegistryRef = useRef(new Map()); // hex → L.Marker for imperative 60fps setLatLng
  const latLngCacheRef    = useRef({});         // hex → L.LatLng — mutated in place to avoid per-frame allocation

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
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Derived: radar-detected only (exclude pure ADS-B not seen by radar) ── */
  const radarAircraft = useMemo(
    () => displayAircraft.filter((ac) => ac.position_source || ac.multinode),
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
    () => displayAircraft.filter((ac) => ac._isTruth && !matchedTruthHexes.has(ac.hex)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [displayAircraft, matchedTruthHexes],
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

  // Detection arcs from accumulated buffer — each detection creates an independently-fading arc.
  // Arcs persist for ~10s after last server update, key = hex-node_id.
  const visibleArcs = useMemo(() => {
    const buf = arcsBufferRef.current;
    const now = Date.now();
    const result = [];
    for (const key of Object.keys(buf)) {
      const entry = buf[key];
      if (now - entry.ts > 10_000) continue;
      const mid = entry.ambiguity_arc[Math.floor(entry.ambiguity_arc.length / 2)];
      if (
        entry.hex === selectedHex ||
        (mid && isPointInViewport(mid[0], mid[1], viewport, 0.5))
      ) {
        result.push({ ...entry, _key: key });
      }
    }
    return result;
    // trailTick fires on every WS message (~1Hz) — replaces `aircraft` dep.
    // visibleArcs no longer recalculates on every setDisplayAircraft (10→2fps).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trailTick, viewport, selectedHex]);

  /* ── Derived: trail for selected aircraft ───────────────────── */
  const visibleTrailEntries = useMemo(() => {
    if (!selectedHex) return [];
    return Object.entries(trailsRef.current).filter(
      ([hex, positions]) => hex === selectedHex && positions.some((p) => isPointInViewport(p[0], p[1], viewport)),
    );
  }, [selectedHex, trailTick, viewport]);

  const selectedTrailPositions = useMemo(() => {
    if (!visibleTrailEntries.length) return [];
    const [, positions] = visibleTrailEntries[0];
    const pts = sampleTrailPositions(positions).map((p) => [p[0], p[1]]);
    // smoothRef is updated at 60fps (vs displayedAircraftRef which is only 2fps)
    // so the trail tip connects exactly to the current smoothed position.
    const animated = selectedHex ? smoothRef.current[selectedHex] : null;
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

  const handleSelectAircraft = useCallback((hex) => {
    setSelectedHex((prev) => {
      const next = prev === hex ? null : hex;
      // Only zoom when selecting a new aircraft, not when deselecting
      if (next !== null) setFocusNonce((n) => n + 1);
      return next;
    });
  }, []);

  const handleSelectNode = useCallback((nodeId) => {
    setSelectedNodeId((prev) => (prev === nodeId ? null : nodeId));
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
        showCoverage={showCoverage}
        showLabels={showLabels}
        showTrails={showTrails}
        showGroundTruth={showGroundTruth}
        onToggleCoverage={() => setShowCoverage((v) => !v)}
        onToggleLabels={() => setShowLabels((v) => !v)}
        onToggleTrails={() => setShowTrails((v) => !v)}
        onToggleGroundTruth={() => setShowGroundTruth((v) => !v)}
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

            {/* Contributing node rings — shown when a multinode-solved aircraft is selected */}
            {selectedAc?.multinode && Array.isArray(selectedAc.contributing_node_ids) &&
              selectedAc.contributing_node_ids.map((nid) => {
                const cn = nodes.find((n) => n.node_id === nid);
                if (!cn) return null;
                return (
                  <CircleMarker
                    key={`contrib-${nid}`}
                    center={[cn.rx_lat, cn.rx_lon]}
                    radius={14}
                    pathOptions={{ color: "#a78bfa", weight: 2.5, fillOpacity: 0, dashArray: "5 3" }}
                  />
                );
              })
            }

            {/* Selected trail — gradient fade; dashed for arc-type tracks */}
            {showTrails && selectedTrailPositions.length >= 2 && (() => {
              const isArcTrack = selectedAc?.position_source === "single_node_ellipse_arc";
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

            {/* Detection arcs — memoized, sourced from raw WS data (2Hz), not smoothed positions */}
            <DetectionArcs visibleArcs={visibleArcs} selectedHex={selectedHex} onSelect={handleSelectAircraft} onSelectNode={handleSelectNode} />
            {/* Aircraft position markers — ADS-B aided (teal icon+arc), multinode (purple icon), uncertain solo (dimmed circle) */}
            {visibleAircraft.map((ac) => {
              const isSelected = ac.hex === selectedHex;
              if (!ac.lat || !ac.lon) return null;
              if (ac.position_source === "solver_adsb_seed" || ac.position_source === "multinode_solve" || ac.multinode) {
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
              }
              if (ac.position_source === "solver_single_node" || ac.position_source === "single_node_ellipse_arc") {
                return (
                  <CircleMarker
                    key={`icon-${ac.hex}`}
                    center={[ac.lat, ac.lon]}
                    radius={isSelected ? 9 : 6}
                    pathOptions={{
                      color: isSelected ? "#fbbf24" : "#64748b",
                      fillColor: isSelected ? "#fbbf24" : "#94a3b8",
                      fillOpacity: isSelected ? 0.6 : 0.35,
                      weight: isSelected ? 2.5 : 1.5,
                    }}
                    eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
                  />
                );
              }
              return null;
            })}

            {/* Anomaly flag rings — pulsing red circle around flagged aircraft */}
            {visibleAircraft
              .filter((ac) => anomalyHexesRef.current.has(ac.ground_truth_hex || ac.hex) && ac.lat && ac.lon)
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
