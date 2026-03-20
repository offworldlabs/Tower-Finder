import { useState, useEffect } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell,
} from "recharts";
import { api } from "../../api/client";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];

export default function AnalyticsPage() {
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

  // Trust distribution chart
  const trustData = summaries.map((n) => ({
    name: (n.node_id || "").slice(-8),
    trust: Math.round((n.trust_score || 0) * 100),
    reputation: Math.round((n.reputation_score || 0) * 100),
  }));

  // Detection share pie chart
  const detectionShare = summaries.map((n, i) => ({
    name: (n.node_id || "").slice(-8),
    value: n.total_detections || 0,
    fill: COLORS[i % COLORS.length],
  }));

  const totalDetections = summaries.reduce((s, n) => s + (n.total_detections || 0), 0);
  const totalFrames = summaries.reduce((s, n) => s + (n.total_frames || 0), 0);

  return (
    <>
      <div className="page-header">
        <h1>Network Analytics</h1>
        <p>Aggregate performance metrics and analysis</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Detections</div>
          <div className="stat-value">{totalDetections.toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Frames</div>
          <div className="stat-value">{totalFrames.toLocaleString()}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Node Count</div>
          <div className="stat-value">{summaries.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Coverage Pairs</div>
          <div className="stat-value">{overlaps.length}</div>
        </div>
      </div>

      <div className="grid-2">
        {/* Trust & reputation bar chart */}
        <div className="card">
          <div className="card-header"><h3>Trust & Reputation by Node</h3></div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={trustData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" stroke="#64748b" tick={{ fontSize: 10 }} />
                  <YAxis stroke="#64748b" tick={{ fontSize: 11 }} domain={[0, 100]} />
                  <Tooltip
                    contentStyle={{
                      background: "#1a2035",
                      border: "1px solid #1e293b",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                  />
                  <Bar dataKey="trust" fill="#3b82f6" name="Trust %" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="reputation" fill="#10b981" name="Reputation %" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        {/* Detection share pie */}
        <div className="card">
          <div className="card-header"><h3>Detection Share</h3></div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={detectionShare}
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    innerRadius={50}
                    dataKey="value"
                    label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
                    labelLine={false}
                  >
                    {detectionShare.map((entry, index) => (
                      <Cell key={index} fill={entry.fill} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      background: "#1a2035",
                      border: "1px solid #1e293b",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      {/* Cross-node analysis */}
      {overlaps.length > 0 && (
        <div className="card">
          <div className="card-header"><h3>Cross-Node Overlap Analysis</h3></div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Node A</th>
                  <th>Node B</th>
                  <th>Jaccard Index</th>
                  <th>Shared Bins</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {overlaps.map((o, i) => {
                  const j = o.jaccard || o.overlap || 0;
                  return (
                    <tr key={i}>
                      <td style={{ fontFamily: "monospace", fontSize: 12 }}>{(o.node_a || "").slice(-8)}</td>
                      <td style={{ fontFamily: "monospace", fontSize: 12 }}>{(o.node_b || "").slice(-8)}</td>
                      <td>{j.toFixed(3)}</td>
                      <td>{o.shared_bins || o.shared || "—"}</td>
                      <td>
                        <span className={`badge ${j > 0.3 ? "online" : j > 0.1 ? "warning" : "offline"}`}>
                          {j > 0.3 ? "Strong" : j > 0.1 ? "Partial" : "Weak"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
