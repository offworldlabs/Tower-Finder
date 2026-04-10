import { useState, useEffect, useRef } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line,
} from "recharts";
import { api } from "../../api/client";

export default function RFEnvironmentPage() {
  const [nodes, setNodes] = useState([]);
  const [selectedNode, setSelectedNode] = useState("");
  const [loading, setLoading] = useState(true);
  const [snrHistory, setSnrHistory] = useState([]);
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchData = () => {
    Promise.all([api.nodes(), api.analytics()])
      .then(([n, a]) => {
        const nodeMap = n.nodes || {};
        const analyticsMap = a?.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]: [string, any]) => ({
          node_id: id,
          ...info,
          _analytics: analyticsMap[id] || {},
        }));
        setNodes(nodeList);
        if (!selectedNode && nodeList.length > 0) {
          setSelectedNode(nodeList[0].node_id);
        }
        // Append to SNR history for the selected node
        const sel = selectedNode || (nodeList[0]?.node_id);
        if (sel) {
          const nodeData = analyticsMap[sel] || {};
          const snr = nodeData.metrics?.avg_snr || 0;
          setSnrHistory((prev) => [
            ...prev.slice(-30),
            {
              time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
              snr: parseFloat(snr.toFixed(1)),
            },
          ]);
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, 5000);
    return () => clearInterval(timerRef.current);
  }, [selectedNode]);

  if (loading) return <div className="empty-state">Loading…</div>;

  const selected = nodes.find((n) => n.node_id === selectedNode) || nodes[0];
  const metrics = selected?._analytics?.metrics || {};
  const freq = selected?.frequency || selected?._analytics?.detection_area?.center_freq;
  const location = selected?.location || {};

  // Build frequency utilization chart from top 20 nodes by SNR
  const freqData = nodes.map((n) => ({
    name: (n.name || n.node_id || "").slice(-10),
    frequency: (n.frequency || n._analytics?.detection_area?.center_freq || 0) / 1e6,
    snr: n._analytics?.metrics?.avg_snr || 0,
  })).sort((a, b) => b.snr - a.snr).slice(0, 20);

  return (
    <>
      <div className="page-header">
        <h1>RF Environment</h1>
        <p>Noise floor, signal strengths, and frequency utilization</p>
      </div>

      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 12 }}>
        <select
          value={selectedNode}
          onChange={(e) => { setSelectedNode(e.target.value); setSnrHistory([]); }}
          style={{
            padding: "8px 12px",
            borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border)",
            fontSize: 13,
            background: "var(--bg-input)",
            color: "var(--text-primary)",
            maxWidth: 300,
          }}
        >
          {nodes.map((n) => (
            <option key={n.node_id} value={n.node_id}>
              {(n.name || n.node_id || "").slice(-16)}
            </option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{nodes.length} nodes</span>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Average SNR</div>
          <div className="stat-value">{(metrics.avg_snr || 0).toFixed(1)} dB</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Frequency</div>
          <div className="stat-value">{freq ? `${(freq / 1e6).toFixed(1)} MHz` : "—"}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Frames</div>
          <div className="stat-value">{(metrics.total_frames || 0).toLocaleString()}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Detections</div>
          <div className="stat-value">{(metrics.total_detections || 0).toLocaleString()}</div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>SNR Trend (Live)</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Updates every 5s</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={snrHistory}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="time" stroke="#94a3b8" tick={{ fontSize: 10 }} />
                  <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: 6, fontSize: 12 }} />
                  <Line type="monotone" dataKey="snr" stroke="#3b82f6" strokeWidth={2} dot={false} name="Avg SNR (dB)" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Signal Strength — Top 20</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{nodes.length} total nodes</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={freqData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" stroke="#94a3b8" tick={{ fontSize: 10 }} />
                  <YAxis stroke="#94a3b8" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: 6, fontSize: 12 }} />
                  <Bar dataKey="snr" fill="#10b981" name="Avg SNR (dB)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h3>Node RF Details</h3></div>
        <div className="card-body">
          <table>
            <tbody>
              <tr><td style={{ color: "var(--text-muted)" }}>Node ID</td><td style={{ fontFamily: "monospace" }}>{selected?.node_id}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Frequency</td><td>{freq ? `${(freq / 1e6).toFixed(3)} MHz` : "Not configured"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Average SNR</td><td>{(metrics.avg_snr || 0).toFixed(2)} dB</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Total Frames Processed</td><td>{(metrics.total_frames || 0).toLocaleString()}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Detection Rate</td><td>{metrics.total_frames ? ((metrics.total_detections / metrics.total_frames) * 100).toFixed(1) + "%" : "—"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>RX Location</td><td>{location.rx_lat && location.rx_lon ? `${location.rx_lat.toFixed(4)}, ${location.rx_lon.toFixed(4)}` : "—"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>TX Location</td><td>{location.tx_lat && location.tx_lon ? `${location.tx_lat.toFixed(4)}, ${location.tx_lon.toFixed(4)}` : "—"}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
