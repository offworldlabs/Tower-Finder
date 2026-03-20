import { useState, useEffect, useRef } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

export default function NetworkHealthPage() {
  const [dashboard, setDashboard] = useState(null);
  const [aircraft, setAircraft] = useState([]);
  const [loading, setLoading] = useState(true);
  const [history, setHistory] = useState([]);
  const timerRef = useRef();

  const fetchAll = () => {
    Promise.all([api.fleetDashboard(), api.aircraft(), api.nodes()])
      .then(([d, a, n]) => {
        setDashboard(d);
        const acList = a.aircraft || [];
        setAircraft(acList);
        // api.nodes() returns {nodes: {id: {...}, ...}, total, connected}
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]) => ({ node_id: id, ...info }));
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
        // Attach nodeList onto dashboard for rendering below
        d._nodeList = nodeList;
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
                <th>Detections</th>
                <th>Tracks</th>
                <th>Trust</th>
                <th>Reputation</th>
                <th>Uptime</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => {
                const id = node.node_id || node.id || "";
                const online = node.status === "online" || node.connected;
                return (
                  <tr key={id}>
                    <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>
                      {id.slice(-12)}
                    </td>
                    <td>
                      <span className={`badge ${online ? "online" : "offline"}`}>
                        {online ? "Online" : "Offline"}
                      </span>
                    </td>
                    <td>{(node.total_detections || node.detections || 0).toLocaleString()}</td>
                    <td>{node.total_tracks || node.tracks || 0}</td>
                    <td>{((node.trust_score || 0) * 100).toFixed(0)}%</td>
                    <td>{((node.reputation_score || 0) * 100).toFixed(0)}%</td>
                    <td>{formatUptime(node.uptime_s || node.uptime || 0)}</td>
                  </tr>
                );
              })}
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
