import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

export default function OverviewPage() {
  const [nodes, setNodes] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [aircraftCount, setAircraftCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    Promise.all([api.nodes(), api.analytics(), api.aircraft()])
      .then(([n, a, ac]) => {
        // n.nodes is a dict {node_id: {status, ...}}
        const nodeMap = n.nodes || {};
        // a.nodes is a dict {node_id: {trust, metrics, detection_area, reputation}}
        const analyticsMap = a?.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]: [string, any]) => ({
          node_id: id,
          ...info,
          _analytics: analyticsMap[id] || {},
        }));
        setNodes(nodeList);
        setAnalytics(a);
        setAircraftCount((ac.aircraft || []).length);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodeList = Array.isArray(nodes) ? nodes : [];
  const onlineCount = nodeList.filter((n) => n.status !== "disconnected" && n.status != null).length;
  // detection_area.n_detections is the most reliably populated counter
  const totalFrameDetections = nodeList.reduce(
    (s, n) => s + (n._analytics?.metrics?.total_detections || n._analytics?.detection_area?.n_detections || 0),
    0,
  );

  // Build a simple detection-over-index chart from node data
  const chartData = nodeList.map((n, i) => ({
    name: n.name || n.node_id || `Node ${i + 1}`,
    detections: n._analytics?.metrics?.total_detections || n._analytics?.detection_area?.n_detections || 0,
    tracks: n._analytics?.metrics?.total_tracks || 0,
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
          <div className="stat-label">Live Aircraft</div>
          <div className="stat-value">{aircraftCount.toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Frame Detections</div>
          <div className="stat-value">{totalFrameDetections.toLocaleString()}</div>
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
                  <span>{(node._analytics?.metrics?.total_detections || node._analytics?.detection_area?.n_detections || 0).toLocaleString()}</span>
                  <span className="meta-label">Tracks</span>
                  <span>{node._analytics?.metrics?.total_tracks || 0}</span>
                  <span className="meta-label">Uptime</span>
                  <span>{formatUptime(node._analytics?.metrics?.uptime_s || 0)}</span>
                  <span className="meta-label">Avg SNR</span>
                  <span>{(node._analytics?.metrics?.avg_snr || 0).toFixed(1)} dB</span>
                  <span className="meta-label">Heartbeat</span>
                  <span>{formatRelativeTime(node.last_heartbeat)}</span>
                  <span className="meta-label">Config</span>
                  <span style={{ fontFamily: "monospace", fontSize: 11 }}>{node.config_hash ? node.config_hash.slice(0, 8) : "—"}</span>
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

function formatRelativeTime(isoStr) {
  if (!isoStr) return "—";
  const diffS = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diffS < 5) return "just now";
  if (diffS < 60) return `${diffS}s ago`;
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
  return `${Math.floor(diffS / 3600)}h ago`;
}
