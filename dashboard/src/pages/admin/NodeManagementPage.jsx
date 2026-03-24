import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";

const PAGE_SIZE = 25;

export default function NodeManagementPage() {
  const [nodes, setNodes] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    Promise.all([api.nodes(), api.analytics()])
      .then(([n, a]) => {
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]) => ({ node_id: id, ...info }));
        setNodes(nodeList);
        setAnalytics(a);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const rawSummaries = analytics?.nodes || {};
  const summaries = Array.isArray(rawSummaries) ? rawSummaries : Object.values(rawSummaries);
  const summaryMap = {};
  summaries.forEach((s) => { summaryMap[s.node_id] = s; });

  const filtered = search
    ? nodes.filter((n) => ((n.node_id || n.id || n.name || "")).toLowerCase().includes(search.toLowerCase()))
    : nodes;
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

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
            {nodes.filter((n) => n.status !== "disconnected" && n.status != null).length}
          </div>
        </div>
        <div className="stat-card error">
          <div className="stat-label">Offline</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status === "disconnected" || n.status == null).length}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <input
          type="text"
          placeholder="Search nodes…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          style={{ padding: "6px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, width: 260 }}
        />
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Showing {paged.length} of {filtered.length} nodes
        </span>
      </div>

      <div className="node-grid">
        {paged.map((node) => {
          const id = node.node_id || node.id;
          const online = node.status !== "disconnected" && node.status != null;
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
                <span className="meta-label">Frequency</span>
                <span>{node.frequency ? `${(node.frequency / 1e6).toFixed(2)} MHz` : "—"}</span>
                <span className="meta-label">Detections</span>
                <span>{(summary.metrics?.total_detections || summary.detection_area?.n_detections || 0).toLocaleString()}</span>
                <span className="meta-label">Frames</span>
                <span>{(summary.metrics?.total_frames || 0).toLocaleString()}</span>
                <span className="meta-label">Trust</span>
                <span>{((summary.trust?.trust_score || 0) * 100).toFixed(0)}%</span>
                <span className="meta-label">Reputation</span>
                <span>{((summary.reputation?.reputation || 0) * 100).toFixed(0)}%</span>
                <span className="meta-label">Avg SNR</span>
                <span>{(summary.metrics?.avg_snr || 0).toFixed(1)} dB</span>
                <span className="meta-label">Uptime</span>
                <span>{formatUptime(summary.metrics?.uptime_s || 0)}</span>
              </div>
            </div>
          );
        })}
      </div>

      {totalPages > 1 && (
        <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 12 }}>
          <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
          <span style={{ fontSize: 12 }}>Page {page + 1} of {totalPages}</span>
          <button className="btn btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      )}
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
