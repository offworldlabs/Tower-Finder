import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

export default function OverviewPage() {
  const [nodes, setNodes] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    Promise.all([api.nodes(), api.analytics()])
      .then(([n, a]) => {
        // n.nodes is a dict {node_id: {status, ...}}
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]) => ({ node_id: id, ...info }));
        setNodes(nodeList);
        setAnalytics(a);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodeList = Array.isArray(nodes) ? nodes : [];
  const onlineCount = nodeList.filter((n) => n.status !== "disconnected" && n.status != null).length;
  const totalDetections = nodeList.reduce((s, n) => s + (n.total_detections || n.detections || 0), 0);
  const totalTracks = nodeList.reduce((s, n) => s + (n.total_tracks || n.tracks || 0), 0);

  // Build a simple detection-over-index chart from node data
  const chartData = nodeList.map((n, i) => ({
    name: n.name || n.node_id || `Node ${i + 1}`,
    detections: n.total_detections || n.detections || 0,
    tracks: n.total_tracks || n.tracks || 0,
  }));

  return (
    <>
      <div className="page-header">
        <h1>My Nodes Overview</h1>
        <p>Monitor your passive radar nodes in real time</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Nodes Online</div>
          <div className="stat-value">{onlineCount} / {nodeList.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Total Detections</div>
          <div className="stat-value">{totalDetections.toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active Tracks</div>
          <div className="stat-value">{totalTracks.toLocaleString()}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Network Nodes</div>
          <div className="stat-value">{nodeList.length}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Detections by Node</h3>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" stroke="#94a3b8" tick={{ fontSize: 11 }} />
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
                  <Area
                    type="monotone"
                    dataKey="detections"
                    stroke="#3b82f6"
                    fill="rgba(59,130,246,0.15)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h3>My Nodes</h3>
        </div>
        <div className="node-grid" style={{ padding: 16 }}>
          {nodeList.map((node) => {
            const id = node.node_id || node.id;
            const online = node.status !== "disconnected" && node.status != null;
            return (
              <div
                className="node-card"
                key={id}
                onClick={() => navigate(`/nodes/${id}`)}
              >
                <div className="node-name">
                  <span className={`badge ${online ? "online" : "offline"}`}>
                    {online ? "Online" : "Offline"}
                  </span>
                  {node.name || id}
                </div>
                <div className="node-meta">
                  <span className="meta-label">Detections</span>
                  <span>{(node.total_detections || node.detections || 0).toLocaleString()}</span>
                  <span className="meta-label">Tracks</span>
                  <span>{node.total_tracks || node.tracks || 0}</span>
                  <span className="meta-label">Uptime</span>
                  <span>{formatUptime(node.uptime_s || node.uptime || 0)}</span>
                  <span className="meta-label">Avg SNR</span>
                  <span>{(node.avg_snr || 0).toFixed(1)} dB</span>
                </div>
              </div>
            );
          })}
          {nodeList.length === 0 && (
            <div className="empty-state">No nodes connected yet</div>
          )}
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
