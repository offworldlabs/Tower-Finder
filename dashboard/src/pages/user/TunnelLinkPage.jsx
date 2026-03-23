import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function TunnelLinkPage() {
  const [nodes, setNodes] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.nodes()
      .then((n) => {
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]) => ({
          node_id: id,
          ...info,
        }));
        setNodes(nodeList);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>Tunnel & Local Display</h1>
        <p>Access your node's local radar display remotely</p>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-header"><h3>How It Works</h3></div>
        <div className="card-body">
          <p style={{ color: "var(--text-secondary)", fontSize: 13, lineHeight: 1.8 }}>
            Each Retina node runs a local web display showing real-time radar data.
            When tunnel access is enabled, you can view this display remotely through
            a secure connection, similar to <code style={{ background: "var(--bg-input)", padding: "2px 6px", borderRadius: 3 }}>radar3.retnode.com</code>.
          </p>
          <p style={{ color: "var(--text-secondary)", fontSize: 13, lineHeight: 1.8, marginTop: 8 }}>
            You can also generate a public shareable link to let others view your node's display
            (view-only, no control access).
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>My Nodes</h3>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Node</th>
                <th>Status</th>
                <th>Local Display</th>
                <th>Tunnel Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => {
                const id = node.node_id;
                const online = node.status !== "disconnected" && node.status != null;
                return (
                  <tr key={id}>
                    <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>
                      {node.name || id}
                    </td>
                    <td>
                      <span className={`badge ${online ? "online" : "offline"}`}>
                        {online ? "Online" : "Offline"}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: "var(--text-muted)" }}>
                      {online ? "http://[node-ip]:8080" : "—"}
                    </td>
                    <td>
                      <span className="badge warning">Not Yet Available</span>
                    </td>
                    <td>
                      <button className="btn btn-outline btn-sm" disabled title="Coming soon">
                        Enable Tunnel
                      </button>
                    </td>
                  </tr>
                );
              })}
              {nodes.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", padding: 32 }}>
                    No nodes connected yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header"><h3>Coming Soon</h3></div>
        <div className="card-body">
          <ul style={{ color: "var(--text-secondary)", fontSize: 13, paddingLeft: 20, lineHeight: 2 }}>
            <li>One-click tunnel activation for each node</li>
            <li>Public share link generation (view-only)</li>
            <li>Embedded iframe preview in this dashboard</li>
            <li>Bandwidth usage monitoring per tunnel</li>
          </ul>
        </div>
      </div>
    </>
  );
}
