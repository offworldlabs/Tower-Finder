import { useEffect, useRef, useState, useCallback } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  Circle,
  Polygon,
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

function FitBounds({ aircraft, nodes }) {
  const map = useMap();
  const fitted = useRef(false);

  useEffect(() => {
    if (fitted.current) return;
    const pts = [];
    aircraft.forEach((ac) => {
      if (ac.lat && ac.lon) pts.push([ac.lat, ac.lon]);
    });
    nodes.forEach((n) => {
      if (n.rx_lat && n.rx_lon) pts.push([n.rx_lat, n.rx_lon]);
    });
    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [40, 40] });
      fitted.current = true;
    } else if (pts.length === 1) {
      map.setView(pts[0], 10);
      fitted.current = true;
    }
  }, [aircraft, nodes, map]);

  return null;
}

/* ── Main component ───────────────────────────────────────────── */

export default function LiveAircraftMap() {
  const [aircraft, setAircraft] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [showCoverage, setShowCoverage] = useState(true);
  const [connected, setConnected] = useState(false);
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
        setAircraft(data.aircraft || []);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      // Reconnect after 3s
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
          setAircraft(data.aircraft || []);
        }
      } catch {
        /* ignore */
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [connected]);

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
          Coverage zones
        </label>
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

        <FitBounds aircraft={aircraft} nodes={nodes} />

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

        {/* Aircraft markers */}
        {aircraft.map((ac) =>
          ac.lat && ac.lon ? (
            <Marker
              key={ac.hex}
              position={[ac.lat, ac.lon]}
              icon={makeAircraftIcon(ac)}
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
              </Popup>
            </Marker>
          ) : null
        )}
      </MapContainer>
    </div>
  );
}
