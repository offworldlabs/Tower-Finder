import { useState, useEffect, useRef } from "react";
import { api } from "../../api/client";

export default function AlertsPage() {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef();

  const fetchAlerts = () => {
    api.alerts()
      .then((data) => setAlerts(Array.isArray(data) ? data : []))
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchAlerts();
    timerRef.current = setInterval(fetchAlerts, 15000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const severityClass = { info: "online", warning: "warning", error: "offline", critical: "offline" };
  const warnings = alerts.filter((e) => e.severity === "warning");
  const errors = alerts.filter((e) => e.severity === "error" || e.severity === "critical");

  return (
    <>
      <div className="page-header">
        <h1>Alerts & Notifications</h1>
        <p>Stay informed about your nodes and network events</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Alerts</div>
          <div className="stat-value">{alerts.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Warnings</div>
          <div className="stat-value">{warnings.length}</div>
        </div>
        <div className="stat-card error">
          <div className="stat-label">Errors</div>
          <div className="stat-value">{errors.length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Recent Alerts</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Auto-refreshes every 15s</span>
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
              {alerts.map((ev, i) => (
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
              {alerts.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>
                    No alerts — your nodes are running smoothly!
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
