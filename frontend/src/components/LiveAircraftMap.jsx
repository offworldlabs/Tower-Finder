import { useEffect, useRef, useState, useCallback, useMemo } from "react";
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

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
  /* ── Data feeds ─────────────────────────────────────────────── */
  const {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
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
  const [focusNonce, setFocusNonce] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [viewport, setViewport] = useState(null);

  const animationFrameRef = useRef(null);
  const displayedAircraftRef = useRef({});
  const fixesRef = useRef({}); // hex → last server fix + _fixLat/_fixLon/_fixTs

  /* ── Record server fixes when new WS data arrives ───────────── */
  useEffect(() => {
    const now = Date.now();
    for (const ac of aircraft) {
      if (!ac.lat || !ac.lon) continue;
      fixesRef.current[ac.hex] = { ...ac, _fixLat: ac.lat, _fixLon: ac.lon, _fixTs: now, _updatedAt: now };
    }
    // Drop stale entries no longer in the feed
    for (const hex of Object.keys(fixesRef.current)) {
      if (now - (fixesRef.current[hex]._updatedAt ?? 0) > STALE_AIRCRAFT_MS) {
        delete fixesRef.current[hex];
      }
    }
  }, [aircraft]);

  /* ── Continuous 60fps dead-reckoning animation loop ─────────── */
  useEffect(() => {
    const DEG_PER_M = 1 / 111_320;
    const KNOTS_TO_MS = 0.514444;

    const tick = () => {
      const now = Date.now();
      const result = [];
      for (const fix of Object.values(fixesRef.current)) {
        const elapsed = Math.min((now - fix._fixTs) / 1000, 60);
        const gs = fix.gs || 0;
        if (elapsed > 0 && gs > 0) {
          const gs_m_s = gs * KNOTS_TO_MS;
          const track_rad = (fix.track || 0) * (Math.PI / 180);
          const cos_lat = Math.cos(fix._fixLat * (Math.PI / 180)) || 1e-9;
          result.push({
            ...fix,
            lat: fix._fixLat + gs_m_s * Math.cos(track_rad) * DEG_PER_M * elapsed,
            lon: fix._fixLon + (gs_m_s * Math.sin(track_rad)) / (111_320 * cos_lat) * elapsed,
          });
        } else {
          result.push(fix);
        }
      }
      displayedAircraftRef.current = Object.fromEntries(result.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(result);
      animationFrameRef.current = requestAnimationFrame(tick);
    };

    animationFrameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrameRef.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Derived: truth-only aircraft ───────────────────────────── */
  const matchedTruthHexes = new Set(
    displayAircraft.map((ac) => ac.ground_truth_hex || ac.hex).filter(Boolean),
  );

  const truthOnlyAircraft = Object.entries(groundTruthRef.current)
    .filter(([hex, positions]) => !matchedTruthHexes.has(hex) && Array.isArray(positions) && positions.length > 0)
    .map(([hex, positions]) => {
      const last = positions[positions.length - 1];
      return { hex, lat: last[0], lon: last[1], alt_m: last[2], points: positions.length };
    });

  const debugTruthOnlyAircraft = useMemo(
    () => (showGroundTruth ? truthOnlyAircraft : []),
    [showGroundTruth, truthOnlyAircraft],
  );

  /* ── Derived: viewport culling ──────────────────────────────── */
  const filteredAircraft = useMemo(() => {
    if (!searchQuery.trim()) return displayAircraft;
    const q = searchQuery.trim().toLowerCase();
    return displayAircraft.filter(
      (ac) => (ac.hex || "").toLowerCase().includes(q) || (ac.flight || "").toLowerCase().includes(q),
    );
  }, [displayAircraft, searchQuery]);

  const visibleAircraft = useMemo(
    () => filteredAircraft.filter((ac) => ac.hex === selectedHex || isAircraftInViewport(ac, viewport)),
    [filteredAircraft, selectedHex, viewport],
  );

  const visibleTruthOnlyAircraft = useMemo(
    () => debugTruthOnlyAircraft.filter((ac) => ac.hex === selectedHex || isPointInViewport(ac.lat, ac.lon, viewport)),
    [debugTruthOnlyAircraft, selectedHex, viewport],
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
    ? displayAircraft.find((ac) => ac.hex === selectedHex) || debugTruthOnlyAircraft.find((ac) => ac.hex === selectedHex)
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
        aircraftCount={displayAircraft.length + debugTruthOnlyAircraft.length}
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
          allAircraft={displayAircraft}
          truthOnly={debugTruthOnlyAircraft}
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
            <FitBounds aircraft={displayAircraft} nodes={nodes} selectedHex={selectedHex} focusNonce={focusNonce} />

            {/* Coverage zones — Yagi beam sectors (broadside to TX-RX baseline) */}
            {showCoverage && visibleNodes.map((n) => (
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
            ))}

            {/* Node markers */}
            {visibleNodes.map((n) => (
              <Marker key={`node-${n.node_id}`} position={[n.rx_lat, n.rx_lon]} icon={nodeIcon}>
                <Popup>
                  <strong>{n.node_id}</strong><br />
                  Beam: {n.beam_azimuth_deg}&deg; / {n.beam_width_deg}&deg;<br />
                  Range: {n.max_range_km} km
                </Popup>
              </Marker>
            ))}

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

            {/* Aircraft markers */}
            {visibleAircraft.map((ac) => {
              const isSelected = ac.hex === selectedHex;
              // 1. Ellipse arc — single-node with delay constraint
              if (Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2) {
                return (
                  <Polyline
                    key={ac.hex}
                    positions={ac.ambiguity_arc}
                    pathOptions={{
                      color: isSelected ? "#fbbf24" : (ac.target_class === "drone" ? "#fb923c" : "#2dd4bf"),
                      weight: isSelected ? 4 : 3,
                      opacity: 0.95,
                      lineCap: "round",
                      lineJoin: "round",
                    }}
                    eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
                  />
                );
              }
              // 2. Solver-only position (no verified arc) — dimmed uncertainty marker
              if (ac.position_source === "solver_single_node" && ac.lat && ac.lon) {
                return (
                  <CircleMarker
                    key={ac.hex}
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
              // 3. Normal marker (ADS-B associated or multinode)
              if (ac.lat && ac.lon) {
                const icon = ac.target_class === "drone"
                  ? makeDroneIcon(ac, showLabels, isSelected)
                  : makeAircraftIcon(ac, showLabels, isSelected);
                return (
                  <Marker
                    key={ac.hex}
                    position={[ac.lat, ac.lon]}
                    icon={icon}
                    eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
                  />
                );
              }
              return null;
            })}

            {/* Ground-truth-only markers */}
            {showGroundTruth && visibleTruthOnlyAircraft.map((ac) => (
              <CircleMarker
                key={`truth-only-${ac.hex}`}
                center={[ac.lat, ac.lon]}
                radius={6}
                pathOptions={{ color: "#67e8f9", weight: 2, fillColor: "#22d3ee", fillOpacity: 0.35 }}
                eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
              />
            ))}
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
