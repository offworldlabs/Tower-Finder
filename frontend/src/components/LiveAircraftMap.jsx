import { useEffect, useRef, useState, useCallback, useMemo } from "react";
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

// Fix default icon paths
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl:
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

const API_BASE = "/api";
const ANIMATION_MS = 700;
const STALE_AIRCRAFT_MS = 8000;
const MAX_HISTORY = 150;


function interpolateBearing(start, end, progress) {
  const a = start ?? 0;
  const b = end ?? a;
  let delta = ((b - a + 540) % 360) - 180;
  return (a + delta * progress + 360) % 360;
}


function easeInOutCubic(progress) {
  return progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - Math.pow(-2 * progress + 2, 3) / 2;
}

/* ── Icon factories ───────────────────────────────────────────────── */

function makeAircraftIcon(ac, showLabel, isSelected) {
  const track = ac.track ?? 0;
  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const color = isMultinode ? "#8b5cf6" : hasAdsb ? "#3b82f6" : "#16a34a";
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  const alt = ac.alt_baro ? `FL${Math.round(ac.alt_baro / 100)}` : "";
  const labelHtml = showLabel
    ? `<div class="aircraft-label">${label}${alt ? "<br>" + alt : ""}</div>`
    : "";
  const glow = isSelected ? "filter:drop-shadow(0 0 6px #fbbf24);" : "";
  return L.divIcon({
    className: "aircraft-marker",
    html: `<div style="display:flex;flex-direction:column;align-items:center;${glow}">
      <div style="transform:rotate(${track}deg);color:${color};font-size:22px;line-height:1;text-shadow:0 1px 4px rgba(0,0,0,.6);">&#9650;</div>
      ${labelHtml}
    </div>`,
    iconSize: [80, 40],
    iconAnchor: [40, 12],
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

function FitBounds({ aircraft, nodes, selectedHex, focusNonce }) {
  const map = useMap();
  const initialFitted = useRef(false);
  const userMoved = useRef(false);
  const lastFocusNonce = useRef(null);

  // Detect user-initiated pan/zoom — after that, never auto-fit again unless
  // the user explicitly clicks "Fit" or selects an aircraft (focusNonce bump).
  useEffect(() => {
    const onMove = () => { userMoved.current = true; };
    map.on("dragstart", onMove);
    map.on("zoomstart", onMove);
    return () => {
      map.off("dragstart", onMove);
      map.off("zoomstart", onMove);
    };
  }, [map]);

  useEffect(() => {
    // After initial fit, only re-fit when focusNonce changes (explicit Fit/select).
    const isExplicit = focusNonce !== lastFocusNonce.current;
    if (initialFitted.current && userMoved.current && !isExplicit) return;

    const pts = [];
    if (selectedHex) {
      const ac = aircraft.find((a) => a.hex === selectedHex);
      if (ac?.lat && ac?.lon) pts.push([ac.lat, ac.lon]);
    } else {
      aircraft.forEach((ac) => { if (ac.lat && ac.lon) pts.push([ac.lat, ac.lon]); });
      nodes.forEach((n) => { if (n.rx_lat && n.rx_lon) pts.push([n.rx_lat, n.rx_lon]); });
    }

    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [60, 60], animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    } else if (pts.length === 1) {
      map.setView(pts[0], 10, { animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    }
  }, [aircraft, nodes, selectedHex, focusNonce, map]);

  return null;
}

/* ── Aircraft list sidebar ─────────────────────────────────────── */

function AircraftListPanel({ allAircraft, truthOnly, selectedHex, onSelect, collapsed, onToggleCollapse, searchQuery, onSearchChange }) {
  const rowRefs = useRef({});

  useEffect(() => {
    if (selectedHex && rowRefs.current[selectedHex]) {
      rowRefs.current[selectedHex].scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [selectedHex]);

  const all = useMemo(() => [
    ...allAircraft.map((ac) => ({ ...ac, _isSolved: true })),
    ...truthOnly.map((ac) => ({ ...ac, _isSolved: false })),
  ].sort((a, b) => {
    const altA = a.alt_baro ?? (a.alt_m ? a.alt_m / 0.3048 : 0);
    const altB = b.alt_baro ?? (b.alt_m ? b.alt_m / 0.3048 : 0);
    return altB - altA;
  }), [allAircraft, truthOnly]);

  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return all;
    const q = searchQuery.toLowerCase();
    return all.filter(
      (ac) =>
        (ac.hex || "").toLowerCase().includes(q) ||
        (ac.flight || "").toLowerCase().includes(q)
    );
  }, [all, searchQuery]);

  return (
    <div className={`aircraft-list-panel${collapsed ? " collapsed" : ""}`}>
      <div className="al-header">
        <div className="al-title">
          {!collapsed && <>Aircraft <span className="al-count">{filtered.length}</span></>}
        </div>
        <button className="al-collapse-btn" onClick={onToggleCollapse} title={collapsed ? "Expand" : "Collapse"}>
          {collapsed ? "▶" : "◀"}
        </button>
      </div>

      {!collapsed && (
        <>
          <div className="al-search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" />
            </svg>
            <input
              type="text"
              placeholder="Search callsign / hex…"
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
            />
            {searchQuery && (
              <button className="al-clear" onClick={() => onSearchChange("")}>×</button>
            )}
          </div>

          <div className="al-list">
            {filtered.length === 0 && <div className="al-empty">No aircraft</div>}
            {filtered.map((ac) => {
              const isSolved = ac._isSolved;
              const isMultinode = ac.multinode;
              const color = !isSolved ? "#22d3ee" : isMultinode ? "#8b5cf6" : "#3b82f6";
              const callsign = ac.flight?.trim() || ac.hex?.slice(-6).toUpperCase() || ac.hex;
              const alt = ac.alt_baro
                ? `FL${Math.round(ac.alt_baro / 100)}`
                : ac.alt_m
                  ? `FL${Math.round(ac.alt_m / 0.3048 / 100)}`
                  : "—";
              const spd = ac.gs ? `${Math.round(ac.gs)}kt` : "—";
              const hdg = ac.track ? `${Math.round(ac.track)}°` : "";
              const isSelected = ac.hex === selectedHex;
              return (
                <div
                  key={ac.hex}
                  ref={(el) => { rowRefs.current[ac.hex] = el; }}
                  className={`al-row${isSelected ? " selected" : ""}${!isSolved ? " truth-only" : ""}`}
                  onClick={() => onSelect(ac.hex)}
                >
                  <div className="al-indicator" style={{ background: color }} />
                  <span
                    className="al-icon"
                    style={{ color, transform: `rotate(${ac.track ?? 0}deg)` }}
                  >▲</span>
                  <div className="al-info">
                    <span className="al-callsign">{callsign}</span>
                    <span className="al-sub">
                      {isSolved ? (isMultinode ? `Multi·${ac.n_nodes}N` : "ADS-B") : "Truth"}
                    </span>
                  </div>
                  <div className="al-stats">
                    <span className="al-alt">{alt}</span>
                    <span className="al-spd">{spd}{hdg ? ` · ${hdg}` : ""}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

/* ── Aircraft detail side panel ───────────────────────────────── */

function AircraftDetailPanel({ ac, onClose, groundTruth, trails, computeError }) {
  if (!ac) return null;
  const err = computeError(ac.hex, ac);
  const gtHex = ac.ground_truth_hex || ac.hex;
  const gtTrail = groundTruth[gtHex];
  const solvedPts = (trails[ac.hex] || []).length;
  const truthPts = gtTrail?.length || 0;
  const gtLast = gtTrail?.length ? gtTrail[gtTrail.length - 1] : null;
  const altErrFt = gtLast ? Math.abs((ac.alt_baro || 0) - gtLast[2] / 0.3048) : null;

  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const sourceLabel = isMultinode
    ? `Multi-node (${ac.n_nodes}N)`
    : hasAdsb ? "ADS-B" : ac.type || "Unknown";
  const sourceBadge = isMultinode ? "multinode" : hasAdsb ? "adsb" : "other";
  const isTruthOnly = !ac.type && !ac.flight;

  return (
    <div className="detail-panel">
      <div className="detail-panel-header">
        <h3>{ac.flight?.trim() || ac.hex}</h3>
        <button className="close-btn" onClick={onClose} title="Close">&times;</button>
      </div>
      <div className="detail-panel-body">
        <div className="detail-section">
          <div className="detail-section-title">Identity</div>
          <div className="detail-field">
            <span className="detail-label">HEX</span>
            <span className="detail-hex-badge">{ac.hex}</span>
          </div>
          {!isTruthOnly && (
            <>
              <div className="detail-field">
                <span className="detail-label">Callsign</span>
                <span className="detail-value">{ac.flight?.trim() || "\u2014"}</span>
              </div>
              <div className="detail-field">
                <span className="detail-label">Source</span>
                <span className={`detail-source-badge ${sourceBadge}`}>{sourceLabel}</span>
              </div>
            </>
          )}
          {isTruthOnly && (
            <div className="detail-field">
              <span className="detail-label">Status</span>
              <span className="detail-source-badge other">Ground truth only</span>
            </div>
          )}
        </div>

        <div className="detail-section">
          <div className="detail-section-title">Position</div>
          <div className="detail-field">
            <span className="detail-label">Latitude</span>
            <span className="detail-value">{ac.lat?.toFixed(5) ?? "\u2014"}</span>
          </div>
          <div className="detail-field">
            <span className="detail-label">Longitude</span>
            <span className="detail-value">{ac.lon?.toFixed(5) ?? "\u2014"}</span>
          </div>
          <div className="detail-field">
            <span className="detail-label">Altitude</span>
            <span className="detail-value">
              {ac.alt_baro != null
                ? `${ac.alt_baro.toLocaleString()} ft`
                : ac.alt_m != null
                  ? `${Math.round(ac.alt_m / 0.3048).toLocaleString()} ft`
                  : "\u2014"}
            </span>
          </div>
          {!isTruthOnly && (
            <>
              <div className="detail-field">
                <span className="detail-label">Speed</span>
                <span className="detail-value">{ac.gs != null ? `${ac.gs} kts` : "\u2014"}</span>
              </div>
              <div className="detail-field">
                <span className="detail-label">Heading</span>
                <span className="detail-value">{ac.track != null ? `${ac.track.toFixed(0)}\u00b0` : "\u2014"}</span>
              </div>
            </>
          )}
        </div>

        {isMultinode && (
          <div className="detail-section">
            <div className="detail-section-title">Multi-node</div>
            <div className="detail-field">
              <span className="detail-label">Nodes</span>
              <span className="detail-value">{ac.n_nodes}</span>
            </div>
            <div className="detail-field">
              <span className="detail-label">RMS Delay</span>
              <span className="detail-value">{ac.rms_delay ?? "\u2014"} \u03bcs</span>
            </div>
            <div className="detail-field">
              <span className="detail-label">RMS Doppler</span>
              <span className="detail-value">{ac.rms_doppler ?? "\u2014"} Hz</span>
            </div>
          </div>
        )}

        <div className="detail-section">
          <div className="detail-section-title">Accuracy</div>
          <div className="detail-field">
            <span className="detail-label">Solved pts</span>
            <span className="detail-value">{solvedPts}</span>
          </div>
          <div className="detail-field">
            <span className="detail-label">Truth pts</span>
            <span className="detail-value">{truthPts}</span>
          </div>
          {err !== null && (
            <div className="detail-field">
              <span className="detail-label">Pos Error</span>
              <span className={`detail-value ${err < 2 ? "good" : err < 5 ? "warn" : "bad"}`}>
                {err.toFixed(2)} km
              </span>
            </div>
          )}
          {altErrFt !== null && (
            <div className="detail-field">
              <span className="detail-label">Alt Error</span>
              <span className="detail-value">{Math.round(altErrFt)} ft</span>
            </div>
          )}
        </div>

        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Trail</div>
            <div className="detail-field">
              <span className="detail-label">Points</span>
              <span className="detail-value">{ac.points || 0}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
  const [aircraft, setAircraft] = useState([]);
  const [displayAircraft, setDisplayAircraft] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [showCoverage, setShowCoverage] = useState(false);
  const [showTrails, setShowTrails] = useState(true);
  const [showGroundTruth, setShowGroundTruth] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [connected, setConnected] = useState(false);
  const [selectedHex, setSelectedHex] = useState(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

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
  const animationFrameRef = useRef(null);
  const displayedAircraftRef = useRef({});
  const pausedRef = useRef(false);
  const historyRef = useRef([]);

  const matchedTruthHexes = new Set(
    displayAircraft.map((ac) => ac.ground_truth_hex || ac.hex).filter(Boolean)
  );
  const truthOnlyAircraft = Object.entries(groundTruthRef.current)
    .filter(([hex, positions]) => !matchedTruthHexes.has(hex) && Array.isArray(positions) && positions.length > 0)
    .map(([hex, positions]) => {
      const last = positions[positions.length - 1];
      return {
        hex,
        lat: last[0],
        lon: last[1],
        alt_m: last[2],
        points: positions.length,
      };
    });

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

        // Buffer history for playback
        historyRef.current.push({ aircraft: newAircraft, ts: Date.now() });
        if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();

        if (!pausedRef.current) setAircraft(newAircraft);

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
      cancelAnimationFrame(animationFrameRef.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connectWs]);

  useEffect(() => {
    cancelAnimationFrame(animationFrameRef.current);

    if (!aircraft.length) {
      const existing = Object.values(displayedAircraftRef.current || {});
      if (!existing.length) {
        setDisplayAircraft([]);
        return;
      }

      const now = Date.now();
      const fresh = existing.filter((ac) => {
        const updatedAt = ac._updatedAt ?? now;
        return now - updatedAt < STALE_AIRCRAFT_MS;
      });

      displayedAircraftRef.current = Object.fromEntries(fresh.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(fresh);
      return;
    }

    const startByHex = displayedAircraftRef.current;
    const startTs = performance.now();
    const nextByHex = Object.fromEntries(
      aircraft.map((ac) => [ac.hex, { ...ac, _updatedAt: Date.now() }])
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
        const updatedAt = ac._updatedAt ?? 0;
        if (Date.now() - updatedAt < STALE_AIRCRAFT_MS) {
          nextAircraft.push(ac);
        }
      });

      displayedAircraftRef.current = Object.fromEntries(nextAircraft.map((ac) => [ac.hex, ac]));
      setDisplayAircraft(nextAircraft);
      if (progress < 1) {
        animationFrameRef.current = requestAnimationFrame(animate);
      }
    };

    animationFrameRef.current = requestAnimationFrame(animate);

    return () => cancelAnimationFrame(animationFrameRef.current);
  }, [aircraft]);

  // Fallback: poll if WebSocket not available
  useEffect(() => {
    if (connected) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/radar/data/aircraft.json`);
        if (res.ok) {
          const data = await res.json();
          const newAircraft = data.aircraft || [];
          historyRef.current.push({ aircraft: newAircraft, ts: Date.now() });
          if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();
          if (!pausedRef.current) setAircraft(newAircraft);
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
    const gtHex = ac.ground_truth_hex || hex;
    const gtTrail = groundTruthRef.current[gtHex];
    if (!gtTrail || !gtTrail.length) return null;
    const last = gtTrail[gtTrail.length - 1]; // [lat, lon, alt_m, ts]
    const dlat = (ac.lat - last[0]) * 111.0;
    const dlon = (ac.lon - last[1]) * 111.0 * Math.cos((ac.lat * Math.PI) / 180);
    return Math.sqrt(dlat * dlat + dlon * dlon);
  }

  // Search filter
  const filteredAircraft = useMemo(() => {
    if (!searchQuery.trim()) return displayAircraft;
    const q = searchQuery.trim().toLowerCase();
    return displayAircraft.filter(
      (ac) =>
        (ac.hex || "").toLowerCase().includes(q) ||
        (ac.flight || "").toLowerCase().includes(q)
    );
  }, [displayAircraft, searchQuery]);

  const selectedAc = selectedHex
    ? filteredAircraft.find((ac) => ac.hex === selectedHex) ||
      truthOnlyAircraft.find((ac) => ac.hex === selectedHex)
    : null;

  function handleTogglePause() {
    if (paused) {
      setPaused(false);
      pausedRef.current = false;
    } else {
      setPaused(true);
      pausedRef.current = true;
    }
  }

  function handleHistorySeek(index) {
    if (index >= 0 && index < historyRef.current.length) {
      setDisplayAircraft(historyRef.current[index].aircraft);
    }
  }

  function formatSecondsAgo(ts) {
    const sec = Math.round((Date.now() - ts) / 1000);
    return sec <= 0 ? "now" : `-${sec}s`;
  }

  return (
    <div className="live-map-container">
      {/* ── Top toolbar ── */}
      <div className="live-map-toolbar">
        <span className={`connection-badge ${connected ? "connected" : "disconnected"}`}>
          {connected ? (paused ? "PAUSED" : "LIVE") : "POLL"}
        </span>
        <span className="aircraft-count">
          {displayAircraft.length + truthOnlyAircraft.length} aircraft
        </span>

        <div className="toolbar-separator" />

        <button className={`toggle-btn${showCoverage ? " active" : ""}`} onClick={() => setShowCoverage((v) => !v)}>Coverage</button>
        <button className={`toggle-btn${showLabels ? " active" : ""}`} onClick={() => setShowLabels((v) => !v)}>Labels</button>
        <button className={`toggle-btn${showTrails ? " active" : ""}`} onClick={() => setShowTrails((v) => !v)}>Trails</button>
        <button className={`toggle-btn${showGroundTruth ? " active" : ""}`} onClick={() => setShowGroundTruth((v) => !v)}>Truth</button>

        <div className="toolbar-separator" />

        <button className={`toggle-btn${paused ? " active" : ""}`} onClick={handleTogglePause}>
          {paused ? "▶ Resume" : "⏸ Pause"}
        </button>
        <button className="toggle-btn" onClick={() => setFocusNonce((n) => n + 1)}>◎ Fit</button>

        <span className="map-legend">
          <span className="legend-item"><span className="legend-dot" style={{ background: "#3b82f6" }} /> ADS-B</span>
          <span className="legend-item"><span className="legend-dot" style={{ background: "#8b5cf6" }} /> Multi</span>
          <span className="legend-item"><span className="legend-dot" style={{ background: "#22d3ee" }} /> Truth</span>
          <span className="legend-item"><span className="legend-dot" style={{ background: "#ef4444" }} /> Node</span>
        </span>
      </div>

      {/* ── Body: sidebar + map ── */}
      <div className="live-map-body">
        <AircraftListPanel
          allAircraft={displayAircraft}
          truthOnly={truthOnlyAircraft}
          selectedHex={selectedHex}
          onSelect={(hex) => {
            setSelectedHex((prev) => (prev === hex ? null : hex));
            setFocusNonce((n) => n + 1);
          }}
          collapsed={sidebarCollapsed}
          onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
        />

        <div className="live-map-area">
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
              aircraft={displayAircraft}
              nodes={nodes}
              selectedHex={selectedHex}
              focusNonce={focusNonce}
            />

            {/* Coverage zones */}
            {showCoverage && nodes.map((n) => (
              <Polygon
                key={`cone-${n.node_id}`}
                positions={beamConePositions(n.rx_lat, n.rx_lon, n.beam_azimuth_deg, n.beam_width_deg, n.max_range_km)}
                pathOptions={{ color: "#ef4444", fillColor: "#ef4444", fillOpacity: 0.08, weight: 1, dashArray: "4 4" }}
              />
            ))}

            {/* Node markers */}
            {nodes.map((n) => (
              <Marker key={`node-${n.node_id}`} position={[n.rx_lat, n.rx_lon]} icon={nodeIcon}>
                <Popup>
                  <strong>{n.node_id}</strong><br />
                  Beam: {n.beam_azimuth_deg}° / {n.beam_width_deg}°<br />
                  Range: {n.max_range_km} km
                </Popup>
              </Marker>
            ))}

            {/* Solved track trails (amber) */}
            {showTrails && Object.entries(trailsRef.current).map(([hex, positions]) => {
              const pts = positions.map((p) => [p[0], p[1]]);
              const animated = displayedAircraftRef.current[hex];
              if (animated?.lat && animated?.lon) {
                const last = pts[pts.length - 1];
                if (!last || Math.abs(last[0] - animated.lat) > 0.00001 || Math.abs(last[1] - animated.lon) > 0.00001) {
                  pts.push([animated.lat, animated.lon]);
                }
              }
              if (pts.length < 2) return null;
              const isSelected = hex === selectedHex;
              return (
                <>
                  <Polyline key={`trail-shadow-${hex}-${trailTick}`} positions={pts} pathOptions={{ color: "#111827", weight: isSelected ? 8 : 6, opacity: 0.55 }} />
                  <Polyline key={`trail-${hex}-${trailTick}`} positions={pts} pathOptions={{ color: isSelected ? "#fde68a" : "#f59e0b", weight: isSelected ? 5 : 4, opacity: isSelected ? 1 : 0.92 }} />
                </>
              );
            })}

            {/* Ground truth trails (cyan dashed) */}
            {showGroundTruth && Object.entries(groundTruthRef.current).map(([hex, positions]) => {
              if (!Array.isArray(positions) || positions.length < 2) return null;
              const pts = positions.map((p) => [p[0], p[1]]);
              const isSelected = hex === selectedHex;
              return (
                <>
                  <Polyline key={`gt-shadow-${hex}-${trailTick}`} positions={pts} pathOptions={{ color: "#082f49", weight: isSelected ? 7 : 5, opacity: 0.45, dashArray: "6 5" }} />
                  <Polyline key={`gt-${hex}-${trailTick}`} positions={pts} pathOptions={{ color: "#22d3ee", weight: isSelected ? 4 : 3, opacity: isSelected ? 0.95 : 0.8, dashArray: "6 5" }} />
                </>
              );
            })}

            {/* Aircraft markers */}
            {filteredAircraft.map((ac) =>
              ac.lat && ac.lon ? (
                <Marker
                  key={ac.hex}
                  position={[ac.lat, ac.lon]}
                  icon={makeAircraftIcon(ac, showLabels, ac.hex === selectedHex)}
                  eventHandlers={{
                    click: () => {
                      setSelectedHex((prev) => (prev === ac.hex ? null : ac.hex));
                      setFocusNonce((n) => n + 1);
                    },
                  }}
                />
              ) : null
            )}

            {/* Ground-truth-only markers */}
            {showGroundTruth && truthOnlyAircraft.map((ac) => (
              <CircleMarker
                key={`truth-only-${ac.hex}`}
                center={[ac.lat, ac.lon]}
                radius={6}
                pathOptions={{ color: "#67e8f9", weight: 2, fillColor: "#22d3ee", fillOpacity: 0.35 }}
                eventHandlers={{
                  click: () => {
                    setSelectedHex((prev) => (prev === ac.hex ? null : ac.hex));
                    setFocusNonce((n) => n + 1);
                  },
                }}
              />
            ))}
          </MapContainer>

          {/* Detail panel (absolute inside map area) */}
          {selectedAc && (
            <AircraftDetailPanel
              ac={selectedAc}
              onClose={() => setSelectedHex(null)}
              groundTruth={groundTruthRef.current}
              trails={trailsRef.current}
              computeError={computeError}
            />
          )}

          {/* Playback bar when paused */}
          {paused && historyRef.current.length > 0 && (
            <div className="playback-bar">
              <span className="playback-time">
                {formatSecondsAgo(historyRef.current[0].ts)}
              </span>
              <input
                type="range"
                min={0}
                max={historyRef.current.length - 1}
                defaultValue={historyRef.current.length - 1}
                onChange={(e) => handleHistorySeek(Number(e.target.value))}
              />
              <span className="playback-time">
                {formatSecondsAgo(historyRef.current[historyRef.current.length - 1].ts)}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
