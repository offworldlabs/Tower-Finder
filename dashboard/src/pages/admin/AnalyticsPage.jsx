import { useState, useEffect, useRef } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line,
} from "recharts";
import { api } from "../../api/client";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];

export default function AnalyticsPage() {
  const [analytics, setAnalytics] = useState(null);
  const [overlaps, setOverlaps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [trend, setTrend] = useState([]);
  const timerRef = useRef();

  const fetchData = () => {
    Promise.all([api.analytics(), api.overlaps()])
      .then(([a, o]) => {
        setAnalytics(a);
        setOverlaps(Array.isArray(o) ? o : o.overlaps || []);
        const rawNodes = a?.nodes || {};
        const summaries = Array.isArray(rawNodes) ? rawNodes : Object.values(rawNodes);
        const totalDet = summaries.reduce((s, n) => s + (n.metrics?.total_detections || n.detection_area?.n_detections || 0), 0);
        setTrend((prev) => [
          ...prev,
          {
            time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
            detections: totalDet,
            nodes: summaries.length,
          },
        ].slice(-30));
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, 10000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  // analytics.nodes is a dict {node_id: summary} from the backend
  const rawNodes = analytics?.nodes || {};
  const summaries = Array.isArray(rawNodes) ? rawNodes : Object.values(rawNodes);
  const crossNode = analytics?.cross_node || analytics?.cross_node_analysis || {};

  // Trust distribution chart
  const trustData = summaries.map((n) => ({
    name: (n.node_id || "").slice(-8),
    trust: Math.round((n.trust?.trust_score || 0) * 100),
    reputation: Math.round((n.reputation?.reputation || 0) * 100),
  }));

  // Detection share pie chart
  const detectionShare = summaries.map((n, i) => ({
    name: (n.node_id || "").slice(-8),
    value: n.metrics?.total_detections || n.detection_area?.n_detections || 0,
    fill: COLORS[i % COLORS.length],
  }));

  const totalDetections = summaries.reduce((s, n) => s + (n.metrics?.total_detections || n.detection_area?.n_detections || 0), 0);
  const totalFrames = summaries.reduce((s, n) => s + (n.metrics?.total_frames || 0), 0);

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

      {trend.length > 1 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Detection Trend</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Updates every 10s
            </span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend}>
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
                  <Line type="monotone" dataKey="detections" stroke="#3b82f6" strokeWidth={2} dot={false} name="Detections" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      <div className="grid-2">
        {/* Trust & reputation bar chart */}
        <div className="card">
          <div className="card-header"><h3>Trust & Reputation by Node</h3></div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={trustData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" stroke="#94a3b8" tick={{ fontSize: 10 }} />
                  <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} domain={[0, 100]} />
                  <Tooltip
                    contentStyle={{
                      background: "#ffffff",
                      border: "1px solid #e2e8f0",
                      borderRadius: 6,
                      fontSize: 12,
                      color: "#0f172a",
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
                      background: "#ffffff",
                      border: "1px solid #e2e8f0",
                      borderRadius: 6,
                      fontSize: 12,
                      color: "#0f172a",
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
