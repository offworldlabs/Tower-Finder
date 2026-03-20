import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function EventsPage() {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.adminEvents(200)
      .then((data) => setEvents(Array.isArray(data) ? data : []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const severityClass = { info: "online", warning: "warning", error: "offline", critical: "offline" };

  return (
    <>
      <div className="page-header">
        <h1>Events & Alerts</h1>
        <p>Structured event log from the network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">Total Events</div>
          <div className="stat-value">{events.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Warnings</div>
          <div className="stat-value">
            {events.filter((e) => e.severity === "warning").length}
          </div>
        </div>
        <div className="stat-card error">
          <div className="stat-label">Errors</div>
          <div className="stat-value">
            {events.filter((e) => e.severity === "error" || e.severity === "critical").length}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Event Log</h3>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Severity</th>
                <th>Category</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                    {ev.ts ? new Date(ev.ts * 1000).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className={`badge ${severityClass[ev.severity] || "online"}`}>
                      {ev.severity}
                    </span>
                  </td>
                  <td>{ev.category}</td>
                  <td style={{ color: "var(--text-primary)" }}>{ev.message}</td>
                </tr>
              ))}
              {events.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 32 }}>
                    No events recorded yet
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
