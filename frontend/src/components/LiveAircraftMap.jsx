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
  ANIMATION_MS,
  STALE_AIRCRAFT_MS,
  interpolateBearing,
  easeInOutCubic,
  isPointInViewport,
  sampleTrailPositions,
  buildTrailSegments,
  makeAircraftIcon,
  nodeIcon,
  bistaticOvalPositions,
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

  /* ── Animation: smoothly interpolate aircraft positions ───── */
  useEffect(() => {
    cancelAnimationFrame(animationFrameRef.current);

    if (!aircraft.length) {
      const existing = Object.values(displayedAircraftRef.current || {});
      if (!existing.length) { setDisplayAircraft([]); return; }

      const now = Date.now();
      const fresh = existing.filter((ac) => now - (ac._updatedAt ?? now) < STALE_AIRCRAFT_MS);
      displayedAircraftRef.current = Object.fromEntries(fresh.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(fresh);
      return;
    }

    const startByHex = displayedAircraftRef.current;
    const startTs = performance.now();
    const nextByHex = Object.fromEntries(
      aircraft.map((ac) => [ac.hex, { ...ac, _updatedAt: Date.now() }]),
    );

    const animate = (now) => {
      const progress = Math.min(1, (now - startTs) / ANIMATION_MS);
      const eased = easeInOutCubic(progress);

      const nextAircraft = Object.values(nextByHex).map((ac) => {
        const start = startByHex[ac.hex] || ac;
        const startLat = start.lat ?? ac.lat;
        const startLon = start.lon ?? ac.lon;
        return {
          ...ac,
          lat: startLat + ((ac.lat ?? startLat) - startLat) * eased,
          lon: startLon + ((ac.lon ?? startLon) - startLon) * eased,
          track: interpolateBearing(start.track, ac.track, eased),
        };
      });

      Object.values(startByHex).forEach((ac) => {
        if (nextByHex[ac.hex]) return;
        if (Date.now() - (ac._updatedAt ?? 0) < STALE_AIRCRAFT_MS) nextAircraft.push(ac);
      });

      displayedAircraftRef.current = Object.fromEntries(nextAircraft.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(nextAircraft);
      if (progress < 1) animationFrameRef.current = requestAnimationFrame(animate);
    };

    animationFrameRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animationFrameRef.current);
  }, [aircraft]);

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
    () => filteredAircraft.filter((ac) => ac.hex === selectedHex || isPointInViewport(ac.lat, ac.lon, viewport)),
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

            {/* Coverage zones */}
            {showCoverage && visibleNodes.map((n) => (
              <Polygon
                key={`oval-${n.node_id}`}
                positions={bistaticOvalPositions(n.rx_lat, n.rx_lon, n.tx_lat, n.tx_lon, n.max_range_km)}
                pathOptions={{ color: "#ef4444", fillColor: "#ef4444", fillOpacity: 0.07, weight: 1, dashArray: "4 4" }}
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

            {/* Selected trail — gradient fade */}
            {showTrails && selectedTrailPositions.length >= 2 &&
              buildTrailSegments(selectedTrailPositions).map((seg, i) => (
                <Polyline
                  key={`trail-${selectedHex}-seg${i}-${trailTick}`}
                  positions={seg.positions}
                  pathOptions={{ color: "#f59e0b", weight: seg.weight, opacity: seg.opacity, lineCap: "round", lineJoin: "round" }}
                />
              ))
            }

            {/* Aircraft markers */}
            {visibleAircraft.map((ac) =>
              ac.lat && ac.lon ? (
                <Marker
                  key={ac.hex}
                  position={[ac.lat, ac.lon]}
                  icon={makeAircraftIcon(ac, showLabels, ac.hex === selectedHex)}
                  eventHandlers={{ click: () => handleSelectAircraft(ac.hex) }}
                />
              ) : null,
            )}

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
