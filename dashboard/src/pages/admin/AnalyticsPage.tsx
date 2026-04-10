import { useState, useEffect, useRef } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line, Legend,
} from "recharts";
import { api } from "../../api/client";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#14b8a6"];
const TOP_N_CHART = 15;
const PAGE_SIZE = 25;

export default function AnalyticsPage() {
  const [analytics, setAnalytics] = useState(null);
  const [overlaps, setOverlaps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [trend, setTrend] = useState([]);
  const [overlapPage, setOverlapPage] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);

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

  const rawNodes = analytics?.nodes || {};
  const summaries = Array.isArray(rawNodes) ? rawNodes : Object.values(rawNodes);

  // Trust distribution — show top N by trust, sorted descending
  const allTrust = summaries.map((n) => ({
    name: (n.node_id || "").slice(-8),
    trust: Math.round((n.trust?.trust_score || 0) * 100),
    reputation: Math.round((n.reputation?.reputation || 0) * 100),
  })).sort((a, b) => b.trust - a.trust);
  const trustData = allTrust.slice(0, TOP_N_CHART);

  // Detection share — top 10 + "Others" bucket
  const allDetections = summaries.map((n, i) => ({
    name: (n.node_id || "").slice(-8),
    value: n.metrics?.total_detections || n.detection_area?.n_detections || 0,
  })).sort((a, b) => b.value - a.value);
  const topDet = allDetections.slice(0, 10);
  const othersValue = allDetections.slice(10).reduce((s, d) => s + d.value, 0);
  const detectionShare = [
    ...topDet.map((d, i) => ({ ...d, fill: COLORS[i % COLORS.length] })),
    ...(othersValue > 0 ? [{ name: `Others (${allDetections.length - 10})`, value: othersValue, fill: "#94a3b8" }] : []),
  ];

  const totalDetections = summaries.reduce((s, n) => s + (n.metrics?.total_detections || n.detection_area?.n_detections || 0), 0);
  const totalFrames = summaries.reduce((s, n) => s + (n.metrics?.total_frames || 0), 0);

  const overlapPages = Math.ceil(overlaps.length / PAGE_SIZE);
  const pagedOverlaps = overlaps.slice(overlapPage * PAGE_SIZE, (overlapPage + 1) * PAGE_SIZE);

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
        {/* Trust & reputation bar chart — top N */}
        <div className="card">
          <div className="card-header">
            <h3>Trust & Reputation — Top {TOP_N_CHART}</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{summaries.length} total nodes</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={trustData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" stroke="#94a3b8" tick={{ fontSize: 9 }} interval={0} angle={-35} textAnchor="end" height={50} />
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
                  <Legend />
                  <Bar dataKey="trust" fill="#3b82f6" name="Trust %" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="reputation" fill="#10b981" name="Reputation %" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        {/* Detection share donut — top 10 + Others */}
        <div className="card">
          <div className="card-header">
            <h3>Detection Share — Top 10</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{totalDetections.toLocaleString()} total</span>
          </div>
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
                    formatter={(value, name) => [value.toLocaleString(), name]}
                  />
                  <Legend
                    layout="vertical"
                    align="right"
                    verticalAlign="middle"
                    iconSize={10}
                    wrapperStyle={{ fontSize: 11, lineHeight: "18px" }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      {/* Cross-node analysis with pagination */}
      {overlaps.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Cross-Node Overlap Analysis</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {overlaps.length} pairs
            </span>
          </div>
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
                {pagedOverlaps.map((o, i) => {
                  const j = o.jaccard || o.overlap || 0;
                  return (
                    <tr key={overlapPage * PAGE_SIZE + i}>
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
          {overlapPages > 1 && (
            <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 8, padding: "12px 0" }}>
              <button className="btn btn-secondary btn-sm" disabled={overlapPage === 0} onClick={() => setOverlapPage((p) => p - 1)}>← Prev</button>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Page {overlapPage + 1} of {overlapPages}</span>
              <button className="btn btn-secondary btn-sm" disabled={overlapPage >= overlapPages - 1} onClick={() => setOverlapPage((p) => p + 1)}>Next →</button>
            </div>
          )}
        </div>
      )}
    </>
  );
}
