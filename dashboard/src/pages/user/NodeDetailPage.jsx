import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";

export default function NodeDetailPage() {
  const { nodeId } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.nodeAnalytics(nodeId)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [nodeId]);

  if (loading) return <div className="empty-state">Loading…</div>;
  if (!data) return <div className="empty-state">Node not found</div>;

  const metrics = data.metrics || data;
  const trust = data.trust || {};
  const reputation = data.reputation || {};
  const gapStats = metrics.gap_stats || {};

  // Build SNR-like chart from available data
  const barData = [
    { name: "Avg SNR", value: metrics.avg_snr || 0 },
    { name: "Trust", value: (trust.trust_score || 0) * 100 },
    { name: "Reputation", value: (reputation.score || metrics.reputation_score || 0) * 100 },
  ];

  return (
    <>
      <div className="page-header">
        <h1 style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="btn btn-outline btn-sm" onClick={() => navigate(-1)}>← Back</button>
          {data.node_id || nodeId}
        </h1>
        <p>Detailed metrics and trust analysis</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Frames</div>
          <div className="stat-value">{(metrics.total_frames || 0).toLocaleString()}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Total Detections</div>
          <div className="stat-value">{(metrics.total_detections || 0).toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Tracks</div>
          <div className="stat-value">{metrics.total_tracks || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg SNR</div>
          <div className="stat-value">{(metrics.avg_snr || 0).toFixed(1)} dB</div>
        </div>
      </div>

      <div className="grid-2">
        {/* Trust & Reputation */}
        <div className="card">
          <div className="card-header"><h3>Trust & Reputation</h3></div>
          <div className="card-body">
            <div className="chart-container" style={{ height: 200 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" stroke="#64748b" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#64748b" tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{
                      background: "#1a2035",
                      border: "1px solid #1e293b",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                  />
                  <Bar dataKey="value" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table>
              <tbody>
                <tr><td>Trust Score</td><td>{((trust.trust_score || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>ADS-B Matches</td><td>{trust.adsb_matches || 0}</td></tr>
                <tr><td>ADS-B Misses</td><td>{trust.adsb_misses || 0}</td></tr>
                <tr><td>Reputation</td><td>{((reputation.score || metrics.reputation_score || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>Penalties</td><td>{reputation.penalties || 0}</td></tr>
                <tr><td>Rewards</td><td>{reputation.rewards || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        {/* Gap / Timing Stats */}
        <div className="card">
          <div className="card-header"><h3>Timing & Gaps</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr><td>Uptime</td><td>{formatUptime(metrics.uptime_s || 0)}</td></tr>
                <tr><td>Average Gap</td><td>{(gapStats.avg_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Max Gap</td><td>{(gapStats.max_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Gap Std Dev</td><td>{(gapStats.std_gap || 0).toFixed(3)}s</td></tr>
                <tr><td>Total Gaps</td><td>{gapStats.n_gaps || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Detection Area */}
      {data.detection_area && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header"><h3>Detection Area</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr><td>Estimated Range</td><td>{(data.detection_area.estimated_range_km || 0).toFixed(1)} km</td></tr>
                <tr><td>Beam Width</td><td>{(data.detection_area.beam_width_deg || 0).toFixed(1)}°</td></tr>
                <tr><td>ADS-B Validated Positions</td><td>{data.detection_area.validated_positions || 0}</td></tr>
              </tbody>
            </table>
          </div>
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
