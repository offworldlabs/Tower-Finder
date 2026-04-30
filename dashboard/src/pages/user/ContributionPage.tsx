import { useState, useEffect, useRef } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

const PAGE_SIZE = 25;

export default function ContributionPage() {
  const [analytics, setAnalytics] = useState(null);
  const [overlaps, setOverlaps] = useState([]);
  const [leaderboard, setLeaderboard] = useState([]);
  const [loading, setLoading] = useState(true);
  const [overlapPage, setOverlapPage] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchData = () => {
    Promise.all([api.analytics(), api.overlaps(), api.leaderboard().catch(() => [])])
      .then(([a, o, lb]) => {
        setAnalytics(a);
        setOverlaps(Array.isArray(o) ? o : o.overlaps || []);
        setLeaderboard(Array.isArray(lb) ? lb : lb.leaderboard || []);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, 30000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  // analytics.nodes is a dict {node_id: summary} from the backend
  const rawNodes = analytics?.nodes || {};
  const summaries = Array.isArray(rawNodes) ? rawNodes : Object.values(rawNodes);
  const crossNode = analytics?.cross_node || analytics?.cross_node_analysis || {};

  // Build contribution chart — top 20 by detections
  const chartDataAll = summaries.map((n) => ({
    name: (n.node_id || n.name || "").slice(-8),
    detections: n.metrics?.total_detections || n.detection_area?.n_detections || 0,
    trust: Math.round((n.trust?.trust_score || 0) * 100),
  })).sort((a, b) => b.detections - a.detections);
  const chartData = chartDataAll.slice(0, 20);

  const totalDetections = summaries.reduce((s, n) => s + (n.metrics?.total_detections || n.detection_area?.n_detections || 0), 0);
  const avgTrust = summaries.length
    ? summaries.reduce((s, n) => s + (n.trust?.trust_score || 0), 0) / summaries.length
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
            <h3>Detections per Node — Top 20</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{summaries.length} total nodes</span>
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
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{overlaps.length} pairs</span>
          </div>
          {(() => {
            const totalPages = Math.ceil(overlaps.length / PAGE_SIZE);
            const paged = overlaps.slice(overlapPage * PAGE_SIZE, (overlapPage + 1) * PAGE_SIZE);
            return (
              <>
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
                      {paged.map((o, i) => (
                        <tr key={overlapPage * PAGE_SIZE + i}>
                          <td style={{ fontFamily: "monospace" }}>{(o.node_a || "").slice(-8)}</td>
                          <td style={{ fontFamily: "monospace" }}>{(o.node_b || "").slice(-8)}</td>
                          <td>{(o.jaccard || o.overlap || 0).toFixed(3)}</td>
                          <td>{o.shared_bins || o.shared || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {totalPages > 1 && (
                  <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 8, padding: "12px 0" }}>
                    <button className="btn btn-secondary btn-sm" disabled={overlapPage === 0} onClick={() => setOverlapPage((p) => p - 1)}>← Prev</button>
                    <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Page {overlapPage + 1} of {totalPages}</span>
                    <button className="btn btn-secondary btn-sm" disabled={overlapPage >= totalPages - 1} onClick={() => setOverlapPage((p) => p + 1)}>Next →</button>
                  </div>
                )}
              </>
            );
          })()}
        </div>
      )}

      {leaderboard.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-header">
            <h3>Network Rankings</h3>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Node</th>
                  <th>Detections</th>
                  <th>Trust</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.slice(0, 10).map((entry, i) => (
                  <tr key={entry.node_id || i}>
                    <td style={{ fontWeight: 600, color: i < 3 ? "var(--accent)" : "var(--text-muted)" }}>{i + 1}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{(entry.node_id || "").slice(-12)}</td>
                    <td>{(entry.detections || 0).toLocaleString()}</td>
                    <td>{((entry.trust || 0) * 100).toFixed(0)}%</td>
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
