import { useState, useEffect } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

export default function ContributionPage() {
  const [analytics, setAnalytics] = useState(null);
  const [overlaps, setOverlaps] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.analytics(), api.overlaps()])
      .then(([a, o]) => {
        setAnalytics(a);
        setOverlaps(Array.isArray(o) ? o : o.overlaps || []);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const summaries = analytics?.node_summaries || analytics?.nodes || [];
  const crossNode = analytics?.cross_node_analysis || {};

  // Build contribution chart
  const chartData = summaries.map((n) => ({
    name: (n.node_id || n.name || "").slice(-8),
    detections: n.total_detections || 0,
    trust: Math.round((n.trust_score || 0) * 100),
  }));

  const totalDetections = summaries.reduce((s, n) => s + (n.total_detections || 0), 0);
  const avgTrust = summaries.length
    ? summaries.reduce((s, n) => s + (n.trust_score || 0), 0) / summaries.length
    : 0;

  return (
    <>
      <div className="page-header">
        <h1>Network Contribution</h1>
        <p>Your contribution metrics across the passive radar network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Network Detections</div>
          <div className="stat-value">{totalDetections.toLocaleString()}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Avg Trust Score</div>
          <div className="stat-value">{(avgTrust * 100).toFixed(1)}%</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active Nodes</div>
          <div className="stat-value">{summaries.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Correlation Pairs</div>
          <div className="stat-value">{overlaps.length}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Detections per Node</h3>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData}>
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
                  <Bar dataKey="detections" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      {overlaps.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Coverage Overlaps</h3>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Node A</th>
                  <th>Node B</th>
                  <th>Jaccard Index</th>
                  <th>Shared Bins</th>
                </tr>
              </thead>
              <tbody>
                {overlaps.map((o, i) => (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace" }}>{(o.node_a || "").slice(-8)}</td>
                    <td style={{ fontFamily: "monospace" }}>{(o.node_b || "").slice(-8)}</td>
                    <td>{(o.jaccard || o.overlap || 0).toFixed(3)}</td>
                    <td>{o.shared_bins || o.shared || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
