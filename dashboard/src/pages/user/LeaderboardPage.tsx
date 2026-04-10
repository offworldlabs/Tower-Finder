import { useState, useEffect } from "react";
import { api } from "../../api/client";

const PAGE_SIZE = 25;

export default function LeaderboardPage() {
  const [data, setData] = useState(null);
  const [sortBy, setSortBy] = useState("detections");
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.leaderboard()
      .then(setData)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const entries = data?.leaderboard || [];

  // Re-sort locally based on selection
  const sorted = [...entries].sort((a, b) => {
    if (sortBy === "detections") return b.detections - a.detections;
    if (sortBy === "uptime") return b.uptime_s - a.uptime_s;
    if (sortBy === "trust") return b.trust_score - a.trust_score;
    if (sortBy === "snr") return b.avg_snr - a.avg_snr;
    return 0;
  });

  const top3 = sorted.slice(0, 3);

  return (
    <>
      <div className="page-header">
        <h1>Leaderboard & Community</h1>
        <p>Network-wide rankings and community links</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Nodes</div>
          <div className="stat-value">{entries.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Online Now</div>
          <div className="stat-value">{entries.filter((e) => e.online).length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Total Detections</div>
          <div className="stat-value">{entries.reduce((s, e) => s + e.detections, 0).toLocaleString()}</div>
        </div>
      </div>

      {/* Podium for top 3 */}
      {top3.length > 0 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header"><h3>Top Performers</h3></div>
          <div className="card-body" style={{ display: "flex", gap: 16, justifyContent: "center", flexWrap: "wrap" }}>
            {top3.map((entry, i) => (
              <div key={entry.node_id} style={{
                textAlign: "center",
                padding: "20px 24px",
                borderRadius: "var(--radius)",
                border: "1px solid var(--border)",
                background: i === 0 ? "var(--warning-light)" : "var(--bg-card-hover)",
                minWidth: 180,
              }}>
                <div style={{ fontSize: 28, fontWeight: 700, marginBottom: 4 }}>
                  {i === 0 ? "🥇" : i === 1 ? "🥈" : "🥉"}
                </div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                  #{i + 1}
                </div>
                <div style={{ fontSize: 12, fontFamily: "monospace", color: "var(--accent)", marginBottom: 4 }}>
                  {(entry.name || entry.node_id).slice(-12)}
                </div>
                <div style={{ fontSize: 20, fontWeight: 700 }}>{entry.detections.toLocaleString()}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>detections</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Sort control */}
      <div style={{ marginBottom: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "var(--text-muted)" }}>Sort by:</span>
        {["detections", "uptime", "trust", "snr"].map((key) => (
          <button
            key={key}
            className={`btn ${sortBy === key ? "btn-primary" : "btn-secondary"} btn-sm`}
            onClick={() => { setSortBy(key); setPage(0); }}
          >
            {key === "snr" ? "SNR" : key.charAt(0).toUpperCase() + key.slice(1)}
          </button>
        ))}
        <input
          type="text"
          placeholder="Search…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          style={{
            marginLeft: "auto",
            padding: "4px 10px",
            borderRadius: 6,
            border: "1px solid var(--border)",
            background: "var(--bg-input)",
            color: "var(--text-primary)",
            fontSize: 12,
            width: 160,
          }}
        />
      </div>

      <div className="card">
        <div className="card-header"><h3>Rankings</h3></div>
        {(() => {
          const filtered = search
            ? sorted.filter((e) => ((e.name || e.node_id || "")).toLowerCase().includes(search.toLowerCase()))
            : sorted;
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
          const offset = page * PAGE_SIZE;
          return (
            <>
              <div className="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Node</th>
                      <th>Status</th>
                      <th>Detections</th>
                      <th>Tracks</th>
                      <th>Uptime</th>
                      <th>Avg SNR</th>
                      <th>Trust</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paged.map((entry, i) => (
                      <tr key={entry.node_id}>
                        <td style={{ fontWeight: 600, color: "var(--text-primary)" }}>{offset + i + 1}</td>
                        <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>
                          {(entry.name || entry.node_id).slice(-12)}
                        </td>
                        <td>
                          <span className={`badge ${entry.online ? "online" : "offline"}`}>
                            {entry.online ? "Online" : "Offline"}
                          </span>
                        </td>
                        <td>{entry.detections.toLocaleString()}</td>
                        <td>{entry.tracks}</td>
                        <td>{formatUptime(entry.uptime_s)}</td>
                        <td>{entry.avg_snr.toFixed(1)} dB</td>
                        <td>{(entry.trust_score * 100).toFixed(0)}%</td>
                      </tr>
                    ))}
                    {paged.length === 0 && (
                      <tr>
                        <td colSpan={8} style={{ textAlign: "center", padding: 32 }}>No nodes found</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              {totalPages > 1 && (
                <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 8, padding: "12px 0" }}>
                  <button className="btn btn-secondary btn-sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>← Prev</button>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Page {page + 1} of {totalPages} ({filtered.length} nodes)</span>
                  <button className="btn btn-secondary btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>Next →</button>
                </div>
              )}
            </>
          );
        })()}
      </div>

      {/* Community Links */}
      <div className="card" style={{ marginTop: 24 }}>
        <div className="card-header"><h3>Community</h3></div>
        <div className="card-body">
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <a
              href="https://discord.gg/retina"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-primary"
              style={{ display: "inline-flex", alignItems: "center", gap: 8, textDecoration: "none", color: "white" }}
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <path d="M20.317 4.37a19.791 19.791 0 00-4.885-1.515.074.074 0 00-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 00-5.487 0 12.64 12.64 0 00-.617-1.25.077.077 0 00-.079-.037A19.736 19.736 0 003.677 4.37a.07.07 0 00-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 00.031.057 19.9 19.9 0 005.993 3.03.078.078 0 00.084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 00-.041-.106 13.107 13.107 0 01-1.872-.892.077.077 0 01-.008-.128 10.2 10.2 0 00.372-.292.074.074 0 01.077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 01.078.01c.12.098.246.198.373.292a.077.077 0 01-.006.127 12.299 12.299 0 01-1.873.892.077.077 0 00-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 00.084.028 19.839 19.839 0 006.002-3.03.077.077 0 00.032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 00-.031-.03z"/>
              </svg>
              Join Discord
            </a>
            <a
              href="https://retina.fm"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
              style={{ display: "inline-flex", alignItems: "center", gap: 8, textDecoration: "none" }}
            >
              Website
            </a>
          </div>
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
