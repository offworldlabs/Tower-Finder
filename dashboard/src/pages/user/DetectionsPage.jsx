import { useState, useEffect, useRef } from "react";
import { api } from "../../api/client";

export default function DetectionsPage() {
  const [aircraft, setAircraft] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [filterNode, setFilterNode] = useState("");
  const [loading, setLoading] = useState(true);
  const timerRef = useRef();

  useEffect(() => {
    api.nodes()
      .then((n) => {
        const nodeMap = n.nodes || {};
        setNodes(Object.keys(nodeMap));
      })
      .catch(console.error);
  }, []);

  const fetchData = () => {
    api.aircraft()
      .then((data) => setAircraft(data.aircraft || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, 3000);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const filtered = filterNode
    ? aircraft.filter((a) => a.node_id === filterNode || a.source === filterNode)
    : aircraft;

  return (
    <>
      <div className="page-header">
        <h1>Live Detections</h1>
        <p>Real-time aircraft feed from the passive radar network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Aircraft Tracked</div>
          <div className="stat-value">{filtered.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">With ADS-B Match</div>
          <div className="stat-value">
            {filtered.filter((a) => a.flight || a.hex).length}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Network</div>
          <div className="stat-value">{aircraft.length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Detection Feed</h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              value={filterNode}
              onChange={(e) => setFilterNode(e.target.value)}
              style={{
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid var(--border)",
                background: "var(--bg-input)",
                color: "var(--text-primary)",
                fontSize: 12,
              }}
            >
              <option value="">All Nodes</option>
              {nodes.map((nid) => (
                <option key={nid} value={nid}>{nid.slice(-12)}</option>
              ))}
            </select>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Auto-refreshes every 3s
            </span>
          </div>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Hex</th>
                <th>Flight</th>
                <th>Lat</th>
                <th>Lon</th>
                <th>Alt (ft)</th>
                <th>Speed (kt)</th>
                <th>Track</th>
                <th>Seen (s)</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((ac, i) => (
                <tr key={ac.hex || i}>
                  <td style={{ fontFamily: "monospace", color: "var(--accent)" }}>
                    {ac.hex || "—"}
                  </td>
                  <td style={{ fontWeight: 500, color: "var(--text-primary)" }}>
                    {ac.flight?.trim() || "—"}
                  </td>
                  <td>{ac.lat?.toFixed(4) ?? "—"}</td>
                  <td>{ac.lon?.toFixed(4) ?? "—"}</td>
                  <td>{ac.alt_baro ?? ac.altitude ?? "—"}</td>
                  <td>{ac.gs?.toFixed(0) ?? ac.speed ?? "—"}</td>
                  <td>{ac.track?.toFixed(0) ?? "—"}°</td>
                  <td>{ac.seen?.toFixed(0) ?? "—"}</td>
                </tr>
              ))}
              {aircraft.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ textAlign: "center", padding: 32 }}>
                    No detections at this time
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
