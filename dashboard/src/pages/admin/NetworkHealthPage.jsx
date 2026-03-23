import { useState, useEffect, useRef } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { MapContainer, TileLayer, CircleMarker, Popup } from "react-leaflet";
import { api } from "../../api/client";

export default function NetworkHealthPage() {
  const [dashboard, setDashboard] = useState(null);
  const [aircraft, setAircraft] = useState([]);
  const [loading, setLoading] = useState(true);
  const [history, setHistory] = useState([]);
  const timerRef = useRef();

  const fetchAll = () => {
    // Fetch fleet dashboard and node data in parallel; if fleetDashboard fails
    // we still render the node list from nodes/analytics.
    const dashPromise = api.fleetDashboard().catch(() => null);
    Promise.all([dashPromise, api.aircraft(), api.nodes(), api.analytics()])
      .then(([d, a, n, an]) => {
        setDashboard(d);
        const acList = a.aircraft || [];
        setAircraft(acList);
        // api.nodes() returns {nodes: {id: {...}, ...}, total, connected}
        const nodeMap = n.nodes || {};
        // analytics.nodes is {node_id: {trust, metrics, detection_area, reputation, ...}}
        const analyticsMap = an?.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]) => {
          const stats = analyticsMap[id] || {};
          return {
            node_id: id,
            ...info,
            _analytics: stats,
          };
        });
        setHistory((prev) => {
          const next = [
            ...prev,
            {
              time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
              aircraft: acList.length,
              nodes: nodeList.length,
            },
          ].slice(-30);
          return next;
        });
        // Attach nodeList onto dashboard (or a stub) for rendering below
        const dash = d || {};
        dash._nodeList = nodeList;
        setDashboard({ ...dash, _nodeList: nodeList });
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchAll();
    timerRef.current = setInterval(fetchAll, 5000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = dashboard?._nodeList || [];
  const dashNodes = dashboard?.nodes || {}; // {total, active, synthetic, real}
  const tracks = dashboard?.pipeline || {};
  const analyticsData = dashboard?.analytics || {};
  const coc = dashboard?.chain_of_custody || {};
  const onlineNodes = nodes.filter((n) => n.status !== "disconnected" && n.status !== undefined);

  return (
    <>
      <div className="page-header">
        <h1>Network Health</h1>
        <p>Real-time monitoring of the passive radar network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Nodes Online</div>
          <div className="stat-value">{dashNodes.active ?? onlineNodes.length} / {dashNodes.total ?? nodes.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Aircraft Tracked</div>
          <div className="stat-value">{aircraft.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Active Tracks</div>
          <div className="stat-value">{tracks.active_tracks || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">CoC Chains</div>
          <div className="stat-value">{coc.nodes_with_chains || 0}</div>
        </div>
      </div>

      {/* Live trend chart */}
      {history.length > 1 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Live Network Activity</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Updates every 5s
            </span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={history}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="time" stroke="#94a3b8" tick={{ fontSize: 10 }} />
                  <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{
                      background: "#ffffff",
                      border: "1px solid #e2e8f0",
                      borderRadius: 6,
                      fontSize: 12,
                      color: "#0f172a",
                    }}
                  />
                  <Area type="monotone" dataKey="aircraft" stroke="#3b82f6" fill="rgba(59,130,246,0.15)" name="Aircraft" />
                  <Area type="monotone" dataKey="nodes" stroke="#10b981" fill="rgba(16,185,129,0.15)" name="Nodes" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      {/* Node location map */}
      {(() => {
        const geoNodes = nodes.filter((n) => n.location?.rx_lat && n.location?.rx_lon);
        if (geoNodes.length === 0) return null;
        const avgLat = geoNodes.reduce((s, n) => s + n.location.rx_lat, 0) / geoNodes.length;
        const avgLon = geoNodes.reduce((s, n) => s + n.location.rx_lon, 0) / geoNodes.length;
        return (
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="card-header">
              <h3>Node Map</h3>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {geoNodes.length} nodes with location
              </span>
            </div>
            <div className="card-body" style={{ padding: 0, height: 400 }}>
              <MapContainer center={[avgLat, avgLon]} zoom={5} style={{ height: "100%", width: "100%", borderRadius: "0 0 8px 8px" }}>
                <TileLayer
                  attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
                  url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                />
                {geoNodes.map((node) => {
                  const id = node.node_id;
                  const online = node.status !== "disconnected" && node.status != null;
                  return (
                    <CircleMarker
                      key={id}
                      center={[node.location.rx_lat, node.location.rx_lon]}
                      radius={7}
                      fillColor={online ? "#10b981" : "#ef4444"}
                      color={online ? "#059669" : "#dc2626"}
                      weight={2}
                      fillOpacity={0.8}
                    >
                      <Popup>
                        <strong>{node.name || id}</strong><br />
                        Status: {online ? "Online" : "Offline"}<br />
                        {node.frequency ? `Freq: ${(node.frequency / 1e6).toFixed(1)} MHz` : ""}
                      </Popup>
                    </CircleMarker>
                  );
                })}
              </MapContainer>
            </div>
          </div>
        );
      })()}

      {/* Node status grid */}
      <div className="card">
        <div className="card-header">
          <h3>Node Status</h3>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Node ID</th>
                <th>Status</th>
                <th>Last Heartbeat</th>
                <th>Detections</th>
                <th>Avg SNR</th>
                <th>Trust</th>
                <th>Reputation</th>
                <th>Uptime</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => {
                const id = node.node_id || node.id || "";
                const online = node.status !== "disconnected" && node.status != null;
                return (
                  <tr key={id}>
                    <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>
                      {id}
                    </td>
                    <td>
                      <span className={`badge ${online ? "online" : "offline"}`}>
                        {online ? "Online" : "Offline"}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: "var(--text-muted)" }}>
                      {formatRelativeTime(node.last_heartbeat)}
                    </td>
                    <td>{(node._analytics?.metrics?.total_detections || node._analytics?.detection_area?.n_detections || 0).toLocaleString()}</td>
                    <td>{(node._analytics?.metrics?.avg_snr || 0).toFixed(1)} dB</td>
                    <td>{((node._analytics?.trust?.trust_score || 0) * 100).toFixed(0)}%</td>
                    <td>{((node._analytics?.reputation?.reputation || 0) * 100).toFixed(0)}%</td>
                    <td>{formatUptime(node._analytics?.metrics?.uptime_s || 0)}</td>
                  </tr>
                );
              })}
              {nodes.length === 0 && (
                <tr><td colSpan={8} style={{ textAlign: "center", padding: 32 }}>No nodes connected</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function formatUptime(seconds) {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return `${h}h ${m}m`;
}

function formatRelativeTime(isoStr) {
  if (!isoStr) return "—";
  const diffS = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diffS < 5) return "just now";
  if (diffS < 60) return `${diffS}s ago`;
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
  return `${Math.floor(diffS / 3600)}h ago`;
}
