import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";

export default function NodeManagementPage() {
  const [nodes, setNodes] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    Promise.all([api.nodes(), api.analytics()])
      .then(([n, a]) => {
        setNodes(Array.isArray(n) ? n : n.nodes || []);
        setAnalytics(a);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const summaries = analytics?.node_summaries || analytics?.nodes || [];
  const summaryMap = {};
  summaries.forEach((s) => {
    summaryMap[s.node_id] = s;
  });

  return (
    <>
      <div className="page-header">
        <h1>Node Management</h1>
        <p>View and manage all nodes in the network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Nodes</div>
          <div className="stat-value">{nodes.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Online</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status === "online" || n.connected).length}
          </div>
        </div>
        <div className="stat-card error">
          <div className="stat-label">Offline</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status !== "online" && !n.connected).length}
          </div>
        </div>
      </div>

      <div className="node-grid">
        {nodes.map((node) => {
          const id = node.node_id || node.id;
          const online = node.status === "online" || node.connected;
          const summary = summaryMap[id] || {};
          return (
            <div className="node-card" key={id} onClick={() => navigate(`/nodes/${id}`)}>
              <div className="node-name">
                <span className={`badge ${online ? "online" : "offline"}`}>
                  {online ? "Online" : "Offline"}
                </span>
                {node.name || id}
              </div>
              <div className="node-meta">
                <span className="meta-label">Detections</span>
                <span>{(node.total_detections || summary.total_detections || 0).toLocaleString()}</span>
                <span className="meta-label">Frames</span>
                <span>{(node.total_frames || summary.total_frames || 0).toLocaleString()}</span>
                <span className="meta-label">Trust</span>
                <span>{((node.trust_score || summary.trust_score || 0) * 100).toFixed(0)}%</span>
                <span className="meta-label">Reputation</span>
                <span>{((node.reputation_score || summary.reputation_score || 0) * 100).toFixed(0)}%</span>
                <span className="meta-label">Avg SNR</span>
                <span>{(node.avg_snr || summary.avg_snr || 0).toFixed(1)} dB</span>
                <span className="meta-label">Uptime</span>
                <span>{formatUptime(node.uptime_s || summary.uptime_s || 0)}</span>
              </div>
            </div>
          );
        })}
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
