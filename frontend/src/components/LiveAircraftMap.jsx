import { useEffect, useRef, useState, useCallback } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  Circle,
  Polygon,
  Polyline,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import "./LiveAircraftMap.css";

// Fix default icon paths
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl:
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

const API_BASE = "/api";

/* ── Icon factories ───────────────────────────────────────────────── */

function makeAircraftIcon(ac) {
  const track = ac.track ?? 0;
  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const color = isMultinode ? "#8b5cf6" : hasAdsb ? "#3b82f6" : "#16a34a";
  return L.divIcon({
    className: "aircraft-marker",
    html: `<div style="
      transform: rotate(${track}deg);
      color: ${color};
      font-size: 20px;
      line-height: 1;
      text-shadow: 0 1px 3px rgba(0,0,0,.4);
    ">&#9650;</div>`,
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
}

const nodeIcon = L.divIcon({
  className: "node-marker",
  html: `<div style="
    width:12px;height:12px;
    background:#ef4444;
    border:2px solid #fff;
    border-radius:50%;
    box-shadow:0 1px 4px rgba(0,0,0,.3);
  "></div>`,
  iconSize: [12, 12],
  iconAnchor: [6, 6],
});

/* ── Beam cone polygon ─────────────────────────────────────────── */

function beamConePositions(lat, lon, azimuthDeg, beamWidthDeg, rangeKm) {
  const R = 6371;
  const startBearing = azimuthDeg - beamWidthDeg / 2;
  const endBearing = azimuthDeg + beamWidthDeg / 2;
  const points = [[lat, lon]]; // apex
  const steps = 30;
  for (let i = 0; i <= steps; i++) {
    const bearing = startBearing + (endBearing - startBearing) * (i / steps);
    const bRad = (bearing * Math.PI) / 180;
    const d = rangeKm / R;
    const lat1 = (lat * Math.PI) / 180;
    const lon1 = (lon * Math.PI) / 180;
    const lat2 = Math.asin(
      Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(bRad)
    );
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(bRad) * Math.sin(d) * Math.cos(lat1),
        Math.cos(d) - Math.sin(lat1) * Math.sin(lat2)
      );
    points.push([(lat2 * 180) / Math.PI, (lon2 * 180) / Math.PI]);
  }
  points.push([lat, lon]); // close
  return points;
}

/* ── Auto-fit map bounds ──────────────────────────────────────── */

function FitBounds({ aircraft, nodes, trails, selectedHex, focusNonce }) {
  const map = useMap();
  const fitted = useRef(false);
  const lastFocusKey = useRef(null);

  useEffect(() => {
    const pts = [];

    const trailEntries = Object.entries(trails || {});
    const activeTrailEntries = selectedHex
      ? trailEntries.filter(([hex]) => hex === selectedHex)
      : trailEntries;

    activeTrailEntries.forEach(([, positions]) => {
      positions.forEach((p) => {
        if (p[0] && p[1]) pts.push([p[0], p[1]]);
      });
    });

    if (pts.length === 0) {
      const activeAircraft = selectedHex
        ? aircraft.filter((ac) => ac.hex === selectedHex)
        : aircraft;
      activeAircraft.forEach((ac) => {
        if (ac.lat && ac.lon) pts.push([ac.lat, ac.lon]);
      });
    }

    if (pts.length === 0) {
      nodes.forEach((n) => {
        if (n.rx_lat && n.rx_lon) pts.push([n.rx_lat, n.rx_lon]);
      });
    }

    const focusKey = `${selectedHex || "all"}:${focusNonce}:${pts.length}`;
    if (fitted.current && lastFocusKey.current === focusKey) return;

    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [40, 40] });
      fitted.current = true;
      lastFocusKey.current = focusKey;
    } else if (pts.length === 1) {
      map.setView(pts[0], 10);
      fitted.current = true;
      lastFocusKey.current = focusKey;
    }
  }, [aircraft, nodes, trails, selectedHex, focusNonce, map]);

  return null;
}

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
  const [aircraft, setAircraft] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showTrails, setShowTrails] = useState(true);
  const [showGroundTruth, setShowGroundTruth] = useState(true);
  const [connected, setConnected] = useState(false);
  const [selectedHex, setSelectedHex] = useState(null);
  const [focusNonce, setFocusNonce] = useState(0);

  // Trails accumulated locally — keyed by hex
  // Each value: [[lat, lon, alt, ts], ...]
  const trailsRef = useRef({});
  // Ground truth trails from server WS broadcast — keyed by hex
  // Each value: [[lat, lon, alt_m, ts], ...]
  const groundTruthRef = useRef({});
  // Force re-render when trails update (every WS tick)
  const [trailTick, setTrailTick] = useState(0);

  const wsRef = useRef(null);
  const reconnectTimer = useRef(null);

  // Fetch nodes for coverage zones
  useEffect(() => {
    async function loadNodes() {
      try {
        const res = await fetch(`${API_BASE}/radar/analytics`);
        if (!res.ok) return;
        const data = await res.json();
        const nodeList = [];
        for (const [id, info] of Object.entries(data.nodes || {})) {
          const da = info.detection_area;
          if (da) {
            nodeList.push({
              node_id: id,
              rx_lat: da.rx.lat,
              rx_lon: da.rx.lon,
              tx_lat: da.tx.lat,
              tx_lon: da.tx.lon,
              beam_azimuth_deg: da.beam_azimuth_deg,
              beam_width_deg: da.beam_width_deg,
              max_range_km: da.max_range_km,
            });
          }
        }
        setNodes(nodeList);
      } catch {
        /* ignore */
      }
    }
    loadNodes();
    const interval = setInterval(loadNodes, 30000);
    return () => clearInterval(interval);
  }, []);

  // WebSocket connection with reconnect
  const connectWs = useCallback(() => {
    if (wsRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/aircraft`);

    ws.onopen = () => {
      setConnected(true);
    };

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const newAircraft = data.aircraft || [];
        setAircraft(newAircraft);

        // Update ground truth from broadcast
        if (data.ground_truth && typeof data.ground_truth === "object") {
          groundTruthRef.current = data.ground_truth;
        }

        // Update trails — prefer server-provided recent_positions, fall back to local accumulation
        const trails = trailsRef.current;
        const now = Date.now() / 1000;
        for (const ac of newAircraft) {
          if (!ac.lat || !ac.lon) continue;
          const hex = ac.hex;
          if (ac.recent_positions && ac.recent_positions.length > 0) {
            // Use server-maintained history
            trails[hex] = ac.recent_positions;
          } else {
            // Accumulate locally
            const existing = trails[hex] || [];
            const last = existing[existing.length - 1];
            if (
              !last ||
              Math.abs(last[0] - ac.lat) > 0.00005 ||
              Math.abs(last[1] - ac.lon) > 0.00005
            ) {
              const updated = [
                ...existing,
                [ac.lat, ac.lon, ac.alt_baro || 0, now],
              ];
              trails[hex] = updated.slice(-60);
            }
          }
        }
        setTrailTick((t) => t + 1);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      reconnectTimer.current = setTimeout(connectWs, 3000);
    };

    ws.onerror = () => {
      ws.close();
    };

    wsRef.current = ws;
  }, []);

  useEffect(() => {
    connectWs();
    return () => {
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connectWs]);

  // Fallback: poll if WebSocket not available
  useEffect(() => {
    if (connected) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/radar/data/aircraft.json`);
        if (res.ok) {
          const data = await res.json();
          const newAircraft = data.aircraft || [];
          setAircraft(newAircraft);
          if (data.ground_truth) groundTruthRef.current = data.ground_truth;
          // Accumulate trails locally during polling
          const trails = trailsRef.current;
          const now = Date.now() / 1000;
          for (const ac of newAircraft) {
            if (!ac.lat || !ac.lon) continue;
            if (ac.recent_positions && ac.recent_positions.length > 0) {
              trails[ac.hex] = ac.recent_positions;
            } else {
              const existing = trails[ac.hex] || [];
              const last = existing[existing.length - 1];
              if (!last || Math.abs(last[0] - ac.lat) > 0.00005 || Math.abs(last[1] - ac.lon) > 0.00005) {
                trails[ac.hex] = [...existing, [ac.lat, ac.lon, ac.alt_baro || 0, now]].slice(-60);
              }
            }
          }
          setTrailTick((t) => t + 1);
        }
      } catch {
        /* ignore */
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [connected]);

  // Compute position error for a given hex (solved vs ground truth)
  function computeError(hex, ac) {
    const gtTrail = groundTruthRef.current[hex];
    if (!gtTrail || !gtTrail.length) return null;
    const last = gtTrail[gtTrail.length - 1]; // [lat, lon, alt_m, ts]
    const dlat = (ac.lat - last[0]) * 111.0;
    const dlon = (ac.lon - last[1]) * 111.0 * Math.cos((ac.lat * Math.PI) / 180);
    return Math.sqrt(dlat * dlat + dlon * dlon);
  }

  return (
    <div className="live-map-container">
      <div className="live-map-toolbar">
        <span className={`connection-badge ${connected ? "connected" : "disconnected"}`}>
          {connected ? "LIVE" : "POLLING"}
        </span>
        <span className="aircraft-count">{aircraft.length} aircraft</span>
        <label className="coverage-toggle">
          <input
            type="checkbox"
            checked={showCoverage}
            onChange={(e) => setShowCoverage(e.target.checked)}
          />
          Coverage
        </label>
        <label className="coverage-toggle">
          <input
            type="checkbox"
            checked={showTrails}
            onChange={(e) => setShowTrails(e.target.checked)}
          />
          Solved tracks
        </label>
        <label className="coverage-toggle">
          <input
            type="checkbox"
            checked={showGroundTruth}
            onChange={(e) => setShowGroundTruth(e.target.checked)}
          />
          Ground truth
        </label>
        <button
          type="button"
          className="coverage-toggle"
          onClick={() => setFocusNonce((n) => n + 1)}
        >
          Focus traffic
        </button>
        {/* Legend */}
        <span className="map-legend">
          <span className="legend-dot" style={{ background: "#f59e0b" }} /> Solved
          &nbsp;
          <span className="legend-dot" style={{ background: "#22d3ee" }} /> Truth
        </span>
      </div>

      <MapContainer
        center={[34.0, -84.5]}
        zoom={8}
        style={{ height: "100%", width: "100%" }}
      >
        <TileLayer
          attribution='&copy; <a href="https://carto.com">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        />

        <FitBounds
          aircraft={aircraft}
          nodes={nodes}
          trails={trailsRef.current}
          selectedHex={selectedHex}
          focusNonce={focusNonce}
        />

        {/* Coverage zones */}
        {showCoverage &&
          nodes.map((n) => (
            <Polygon
              key={`cone-${n.node_id}`}
              positions={beamConePositions(
                n.rx_lat,
                n.rx_lon,
                n.beam_azimuth_deg,
                n.beam_width_deg,
                n.max_range_km
              )}
              pathOptions={{
                color: "#ef4444",
                fillColor: "#ef4444",
                fillOpacity: 0.08,
                weight: 1,
                dashArray: "4 4",
              }}
            />
          ))}

        {/* Node markers */}
        {nodes.map((n) => (
          <Marker key={`node-${n.node_id}`} position={[n.rx_lat, n.rx_lon]} icon={nodeIcon}>
            <Popup>
              <strong>{n.node_id}</strong>
              <br />
              Beam: {n.beam_azimuth_deg}° / {n.beam_width_deg}°
              <br />
              Range: {n.max_range_km} km
            </Popup>
          </Marker>
        ))}

        {/* ── Solved track trails (yellow/amber) ── */}
        {showTrails &&
          Object.entries(trailsRef.current).map(([hex, positions]) => {
            const pts = positions.map((p) => [p[0], p[1]]);
            if (pts.length < 2) return null;
            const isSelected = hex === selectedHex;
            return (
              <>
                <Polyline
                  key={`trail-shadow-${hex}-${trailTick}`}
                  positions={pts}
                  pathOptions={{
                    color: "#111827",
                    weight: isSelected ? 8 : 6,
                    opacity: 0.55,
                  }}
                />
                <Polyline
                  key={`trail-${hex}-${trailTick}`}
                  positions={pts}
                  pathOptions={{
                    color: isSelected ? "#fde68a" : "#f59e0b",
                    weight: isSelected ? 5 : 4,
                    opacity: isSelected ? 1 : 0.92,
                  }}
                />
              </>
            );
          })}

        {/* ── Ground truth trails (cyan dashed) ── */}
        {showGroundTruth &&
          Object.entries(groundTruthRef.current).map(([hex, positions]) => {
            if (!Array.isArray(positions) || positions.length < 2) return null;
            const pts = positions.map((p) => [p[0], p[1]]);
            const isSelected = hex === selectedHex;
            return (
              <>
                <Polyline
                  key={`gt-shadow-${hex}-${trailTick}`}
                  positions={pts}
                  pathOptions={{
                    color: "#082f49",
                    weight: isSelected ? 7 : 5,
                    opacity: 0.45,
                    dashArray: "6 5",
                  }}
                />
                <Polyline
                  key={`gt-${hex}-${trailTick}`}
                  positions={pts}
                  pathOptions={{
                    color: "#22d3ee",
                    weight: isSelected ? 4 : 3,
                    opacity: isSelected ? 0.95 : 0.8,
                    dashArray: "6 5",
                  }}
                />
              </>
            );
          })}

        {/* Aircraft markers */}
        {aircraft.map((ac) =>
          ac.lat && ac.lon ? (
            <Marker
              key={ac.hex}
              position={[ac.lat, ac.lon]}
              icon={makeAircraftIcon(ac)}
              eventHandlers={{
                click: () => setSelectedHex((prev) => (prev === ac.hex ? null : ac.hex)),
              }}
            >
              <Popup>
                <strong>{ac.flight?.trim() || ac.hex}</strong>
                <br />
                Alt: {ac.alt_baro ?? "?"} ft
                <br />
                GS: {ac.gs ?? "?"} kts &middot; Trk: {ac.track ?? "?"}°
                {ac.multinode && (
                  <>
                    <br />
                    <em>Multi-node ({ac.n_nodes}N)</em>
                    <br />
                    RMS: {ac.rms_delay}μs / {ac.rms_doppler}Hz
                  </>
                )}
                {/* Ground truth comparison */}
                {(() => {
                  const err = computeError(ac.hex, ac);
                  const gtTrail = groundTruthRef.current[ac.hex];
                  if (!gtTrail || !gtTrail.length) return null;
                  const solvedPts = (trailsRef.current[ac.hex] || []).length;
                  const truthPts = gtTrail.length;
                  const gtLast = gtTrail[gtTrail.length - 1];
                  const altErrFt = gtLast
                    ? Math.abs(ac.alt_baro - (gtLast[2] / 0.3048))
                    : null;
                  return (
                    <>
                      <br />
                      <hr style={{ margin: "4px 0", borderColor: "#444" }} />
                      <span style={{ color: "#22d3ee", fontSize: "0.85em" }}>
                        Ground truth comparison
                      </span>
                      <br />
                      <span style={{ color: "#f59e0b" }}>
                        ● Solved: {solvedPts} pts
                      </span>
                      &nbsp;
                      <span style={{ color: "#22d3ee" }}>
                        ● Truth: {truthPts} pts
                      </span>
                      <br />
                      {err !== null && (
                        <>
                          <strong
                            style={{
                              color: err < 2 ? "#4ade80" : err < 5 ? "#fbbf24" : "#f87171",
                            }}
                          >
                            Pos error: {err.toFixed(2)} km
                          </strong>
                          <br />
                        </>
                      )}
                      {altErrFt !== null && (
                        <span style={{ fontSize: "0.85em" }}>
                          Alt error: {Math.round(altErrFt)} ft
                        </span>
                      )}
                    </>
                  );
                })()}
              </Popup>
            </Marker>
          ) : null
        )}
      </MapContainer>
    </div>
  );
}
