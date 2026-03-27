import { useEffect, useRef, useState, useCallback, useMemo, memo } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  CircleMarker,
  Polygon,
  Polyline,
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
  nodeIcon,
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

/* ── AircraftMarker: memoized — icon only rebuilds on selection/label/altitude changes,
      NOT on track/position changes (track is updated via direct DOM in the rAF loop) ── */
const AircraftMarker = memo(function AircraftMarker({ ac, isSelected, showLabels, onSelect }) {
  const altBand = Math.floor((ac.alt_baro ?? 0) / 5000);
  const icon = useMemo(
    () => ac.target_class === "drone"
      ? makeDroneIcon(ac, showLabels, isSelected)
      : makeAircraftIcon(ac, showLabels, isSelected),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ac.hex, isSelected, showLabels, ac.flight, ac.target_class, altBand],
  );
  const handlers = useMemo(() => ({ click: () => onSelect(ac.hex) }), [ac.hex, onSelect]);
  return <Marker position={[ac.lat, ac.lon]} icon={icon} eventHandlers={handlers} />;
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
    historyRef,
    setPaused: setFeedPaused,
  } = useAircraftFeed();

  const nodes = useNodes();

  /* ── Local UI state ─────────────────────────────────────────── */
  const [displayAircraft, setDisplayAircraft] = useState([]);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showTrails, setShowTrails] = useState(true);
  const [showGroundTruth, setShowGroundTruth] = useState(false);
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
      const result = [];

      for (const fix of Object.values(fixesRef.current)) {
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

        smoothRef.current[fix.hex] = { lat: sLat, lon: sLon, track: sTrack };

        // Update rotation directly on the DOM — avoids setIcon() every frame
        // Cache element reference to avoid querySelector on every 16ms frame
        let svgEl = svgElemsRef.current[fix.hex];
        if (!svgEl || !svgEl.isConnected) {
          svgEl = document.querySelector(`.ac-hex-${fix.hex} svg`);
          if (svgEl) svgElemsRef.current[fix.hex] = svgEl;
          else delete svgElemsRef.current[fix.hex];
        }
        if (svgEl) svgEl.style.transform = `rotate(${sTrack.toFixed(1)}deg)`;

        result.push({ ...fix, lat: sLat, lon: sLon, track: sTrack });
      }

      displayedAircraftRef.current = Object.fromEntries(result.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(result);
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

  // trailTick invalidates this when groundTruthRef.current is updated
  const truthOnlyAircraft = useMemo(
    () => Object.entries(groundTruthRef.current)
      .filter(([hex, positions]) => !matchedTruthHexes.has(hex) && Array.isArray(positions) && positions.length > 0)
      .map(([hex, positions]) => {
        const last = positions[positions.length - 1];
        const meta = groundTruthMetaRef.current[hex] || {};
        return {
          hex,
          lat: last[0], lon: last[1], alt_m: last[2],
          alt_baro: Math.round(last[2] / 0.3048),
          gs: Math.round((meta.speed_ms || 0) * 1.94384 * 10) / 10,
          track: meta.heading || 0,
          points: positions.length,
          object_type: meta.object_type,
          is_anomalous: meta.is_anomalous,
          _isTruth: true,
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [matchedTruthHexes, trailTick],
  );

  /* ── Feed truth-only aircraft into fixesRef so the rAF loop dead-reckons them ── */
  useEffect(() => {
    const now = Date.now();
    const truthHexSet = new Set(truthOnlyAircraft.map((a) => a.hex));
    for (const ac of truthOnlyAircraft) {
      if (!ac.lat || !ac.lon) continue;
      const cur = fixesRef.current[ac.hex];
      if (!cur || cur._fixLat !== ac.lat || cur._fixLon !== ac.lon) {
        // New GT position arrived — reset the fix so dead-reckoning starts fresh
        fixesRef.current[ac.hex] = { ...ac, _fixLat: ac.lat, _fixLon: ac.lon, _fixTs: now, _updatedAt: now };
      } else {
        cur._updatedAt = now; // keep alive; position unchanged between GT pushes
      }
    }
    // Remove truth entries that are no longer unmatched
    for (const [hex, fix] of Object.entries(fixesRef.current)) {
      if (fix._isTruth && !truthHexSet.has(hex)) {
        delete fixesRef.current[hex];
        delete smoothRef.current[hex];
      }
    }
  }, [truthOnlyAircraft]);

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

  const visibleTruthOnlyAircraft = useMemo(
    () => showGroundTruth
      ? truthOnlyAircraft.filter((ac) => ac.hex === selectedHex || isPointInViewport(ac.lat, ac.lon, viewport))
      : [],
    [showGroundTruth, truthOnlyAircraft, selectedHex, viewport],
  );

  // Smooth (dead-reckoned at 60fps) version of truth-only aircraft for rendering
  const visibleSmoothTruth = useMemo(
    () => showGroundTruth
      ? displayAircraft.filter(
          (ac) => ac._isTruth &&
                  !matchedTruthHexes.has(ac.hex) &&
                  (ac.hex === selectedHex || isPointInViewport(ac.lat, ac.lon, viewport))
        )
      : [],
    [showGroundTruth, displayAircraft, matchedTruthHexes, selectedHex, viewport],
  );

  const visibleNodes = useMemo(
    () => nodes.filter((node) => isPointInViewport(node.rx_lat, node.rx_lon, viewport, 3)),
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
    if (!visibleTrailEntries.length) return [];
    const [, positions] = visibleTrailEntries[0];
    const pts = sampleTrailPositions(positions).map((p) => [p[0], p[1]]);
    const animated = selectedHex ? displayedAircraftRef.current[selectedHex] : null;
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

  function handleSelectAircraft(hex) {
    setSelectedHex((prev) => (prev === hex ? null : hex));
    setFocusNonce((n) => n + 1);
  }

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
          <MapContainer center={[34.0, -84.5]} zoom={8} style={{ height: "100%", width: "100%" }}>
            <TileLayer
              attribution='&copy; <a href="https://carto.com">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
              url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
            />

            <ViewportTracker onChange={handleViewportChange} />
            <FitBounds aircraft={radarAircraft} nodes={nodes} selectedHex={selectedHex} focusNonce={focusNonce} />

            {/* Coverage zones — empirical polygon when available, else theoretical Yagi sector */}
            {showCoverage && visibleNodes.map((n) => {
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
            })}

            {/* Node markers */}
            {visibleNodes.map((n) => {
              const isNodeSelected = selectedNodeId === n.node_id;
              return (
                <Marker
                  key={`node-${n.node_id}`}
                  position={[n.rx_lat, n.rx_lon]}
                  icon={nodeIcon}
                  eventHandlers={{ click: () => setSelectedNodeId((prev) => (prev === n.node_id ? null : n.node_id)) }}
                >
                  <Popup>
                    <strong>{n.node_id}</strong><br />
                    Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
                    Range: {n.max_range_km} km
                  </Popup>
                </Marker>
              );
            })}

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
                  key={`trail-${selectedHex}-seg${i}-${trailTick}`}
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

            {/* Detection arcs — fade based on age since last detection update */}
            {visibleAircraft
              .filter((ac) => Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2)
              .map((ac) => {
                const isSelected = ac.hex === selectedHex;
                const arcAge = Date.now() - (ac._fixTs || 0);
                const arcOpacity = Math.max(0.05, Math.min(0.95, 1 - Math.max(0, arcAge - 1500) / 5000));
                const arcColor = ac.target_class === "drone"
                  ? "#fb923c"
                  : dopplerColor(ac.doppler_hz ?? 0);
                return (
                  <Polyline
                    key={`arc-${ac.hex}`}
                    positions={ac.ambiguity_arc}
                    pathOptions={{
                      color: arcColor,
                      weight: isSelected ? 5 : 3,
                      opacity: isSelected ? 1 : arcOpacity,
                      lineCap: "round",
                      lineJoin: "round",
                    }}
                    eventHandlers={{ click: () => {
                      handleSelectAircraft(ac.hex);
                      if (ac.node_id) setSelectedNodeId(ac.node_id);
                    }}}
                  />
                );
              })
            }
            {/* Aircraft position markers — ADS-B aided (teal icon+arc), multinode (purple icon), uncertain solo (dimmed circle) */}
            {visibleAircraft.map((ac) => {
              const isSelected = ac.hex === selectedHex;
              if (ac.position_source === "solver_single_node" && ac.lat && ac.lon) {
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
              if (ac.lat && ac.lon && (ac.position_source === "adsb_associated" || ac.position_source === "adsb_node_report" || ac.multinode)) {
                return (
                  <AircraftMarker
                    key={`icon-${ac.hex}`}
                    ac={ac}
                    isSelected={isSelected}
                    showLabels={showLabels}
                    onSelect={handleSelectAircraft}
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

            {/* Ground-truth-only markers — animated via dead-reckoning, color by object type */}
            {showGroundTruth && visibleSmoothTruth.map((ac) => {
              const isAnomGT = ac.is_anomalous;
              const isDroneGT = ac.object_type === "drone";
              const gtColor = isAnomGT ? "#f43f5e" : isDroneGT ? "#f59e0b" : "#22d3ee";
              const gtBorder = isAnomGT ? "#e11d48" : isDroneGT ? "#d97706" : "#67e8f9";
              return (
                <CircleMarker
                  key={`truth-only-${ac.hex}`}
                  center={[ac.lat, ac.lon]}
                  radius={isDroneGT ? 5 : isAnomGT ? 7 : 6}
                  pathOptions={{ color: gtBorder, weight: 2, fillColor: gtColor, fillOpacity: 0.45 }}
                  eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
                />
              );
            })}
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
