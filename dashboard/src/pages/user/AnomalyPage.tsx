import { useState, useEffect, useRef } from "react";
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import { api } from "../../api/client";

interface AnomalyEvent {
  hex: string;
  reason: string;
  lat?: number;
  lon?: number;
  ts?: number;
  flagged_at?: string;
  object_type?: string;
}

interface AnomalyData {
  summary: {
    active_count: number;
    total_events: number;
    unique_hexes: number;
    most_common_type: string | null;
  };
  by_type: Record<string, number>;
  timeline: { ts: number; count: number }[];
  geographic_clusters: { lat: number; lon: number; count: number; dominant_type: string }[];
  recent_events: AnomalyEvent[];
}

const TYPE_COLORS: Record<string, string> = {
  supersonic: "#ef4444",
  instant_acceleration: "#f97316",
  instant_direction_change: "#eab308",
  sustained_orbit: "#8b5cf6",
  position_mismatch: "#3b82f6",
  identity_swap: "#ec4899",
  altitude_jump: "#14b8a6",
  anomalous_behavior: "#6b7280",
};

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export default function AnomalyPage() {
  const [data, setData] = useState<AnomalyData | null>(null);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchData = () => {
    api.anomalies()
      .then((d: AnomalyData) => {
        setData(d);
        setLastUpdated(new Date());
        setStale(false);
      })
      .catch((err) => {
        console.error(err);
        setStale(true);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, 10000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;
  if (!data) return <div className="empty-state">Failed to load anomaly data</div>;

  const { summary, by_type, timeline, geographic_clusters, recent_events } = data;

  const typeData = Object.entries(by_type || {})
    .map(([name, count]) => ({ name, count: count as number }))
    .sort((a, b) => b.count - a.count);

  return (
    <>
      <div className="page-header">
        <h1>Anomaly Monitor</h1>
        <p>Real-time anomaly detection metrics and event log</p>
      </div>

      {/* ── Stats Grid ──────────────────────────────────────── */}
      <div className="stats-grid">
        <div className="stat-card error">
          <div className="stat-label">Active Anomalies</div>
          <div className="stat-value">{summary?.active_count ?? 0}</div>
          <div className="stat-sub">currently flagged</div>
        </div>
        <div className="stat-card accent">
          <div className="stat-label">Total Events</div>
          <div className="stat-value">{summary?.total_events ?? 0}</div>
          <div className="stat-sub">in anomaly log</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Unique Aircraft</div>
          <div className="stat-value">{summary?.unique_hexes ?? 0}</div>
          <div className="stat-sub">distinct hex codes</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Most Common Type</div>
          <div className="stat-value" style={{ fontSize: 18 }}>
            {summary?.most_common_type?.replace(/_/g, " ") ?? "—"}
          </div>
        </div>
      </div>

      {/* ── Charts Row ──────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        {/* Timeline */}
        <div className="card">
          <div className="card-header"><h3>Event Timeline (24h)</h3></div>
          <div style={{ padding: 16, height: 260 }}>
            {timeline && timeline.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={timeline}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis
                    dataKey="ts"
                    tickFormatter={formatTime}
                    stroke="var(--text-muted)"
                    tick={{ fontSize: 11 }}
                  />
                  <YAxis stroke="var(--text-muted)" tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip
                    labelFormatter={(v) => new Date((v as number) * 1000).toLocaleString()}
                    contentStyle={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}
                  />
                  <Area
                    type="monotone"
                    dataKey="count"
                    stroke="#ef4444"
                    fill="#ef444433"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="empty-state">No timeline data</div>
            )}
          </div>
        </div>

        {/* Type Breakdown */}
        <div className="card">
          <div className="card-header"><h3>Breakdown by Type</h3></div>
          <div style={{ padding: 16, height: 260 }}>
            {typeData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={typeData} layout="vertical" margin={{ left: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis type="number" stroke="var(--text-muted)" tick={{ fontSize: 11 }} allowDecimals={false} />
                  <YAxis
                    type="category"
                    dataKey="name"
                    stroke="var(--text-muted)"
                    tick={{ fontSize: 11 }}
                    width={140}
                    tickFormatter={(v) => v.replace(/_/g, " ")}
                  />
                  <Tooltip contentStyle={{ background: "var(--bg-card)", border: "1px solid var(--border)" }} />
                  <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                    {typeData.map((entry, i) => (
                      <Cell key={i} fill={TYPE_COLORS[entry.name] || "#6b7280"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="empty-state">No anomalies detected</div>
            )}
          </div>
        </div>
      </div>

      {/* ── Geographic Clusters ─────────────────────────────── */}
      {geographic_clusters && geographic_clusters.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header">
            <h3>Geographic Hotspots</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Grouped by 0.1° grid</span>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Location</th>
                  <th>Events</th>
                  <th>Dominant Type</th>
                </tr>
              </thead>
              <tbody>
                {geographic_clusters.slice(0, 20).map((c: any, i: number) => (
                  <tr key={i}>
                    <td>#{i + 1}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>
                      {c.lat.toFixed(1)}, {c.lon.toFixed(1)}
                    </td>
                    <td><strong>{c.count}</strong></td>
                    <td>
                      <span
                        className="badge"
                        style={{ background: TYPE_COLORS[c.dominant_type] || "#6b7280", color: "#fff" }}
                      >
                        {c.dominant_type.replace(/_/g, " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Recent Events Table ─────────────────────────────── */}
      <div className="card">
        <div className="card-header">
          <h3>Recent Anomaly Events</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {stale && <span style={{ color: "#f97316", marginRight: 8 }}>⚠ Stale data</span>}
            {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "Auto-refreshes every 10s"}
          </span>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Flagged At</th>
                <th>Hex</th>
                <th>Type</th>
                <th>Lat</th>
                <th>Lon</th>
                <th>Object</th>
              </tr>
            </thead>
            <tbody>
              {[...(recent_events || [])].reverse().map((ev: AnomalyEvent, i: number) => (
                <tr key={`${ev.hex}-${ev.flagged_at ?? i}`}>
                  <td style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                    {ev.flagged_at ? formatDateTime(ev.flagged_at) : "—"}
                  </td>
                  <td style={{ fontFamily: "monospace", fontWeight: 600 }}>{ev.hex}</td>
                  <td>
                    <span
                      className="badge"
                      style={{ background: TYPE_COLORS[ev.reason] || "#6b7280", color: "#fff" }}
                    >
                      {(ev.reason || "unknown").replace(/_/g, " ")}
                    </span>
                  </td>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{ev.lat?.toFixed(4)}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{ev.lon?.toFixed(4)}</td>
                  <td>{ev.object_type || "—"}</td>
                </tr>
              ))}
              {(!recent_events || recent_events.length === 0) && (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>
                    No anomaly events recorded yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
