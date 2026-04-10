import { useState, useEffect } from "react";
import { api } from "../../api/client";

const PAGE_SIZE = 25;

export default function TunnelLinkPage() {
  const [nodes, setNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.nodes()
      .then((n) => {
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]: [string, any]) => ({
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
        <p>Access your node&apos;s local radar display remotely</p>
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
            You can also generate a public shareable link to let others view your node&apos;s display
            (view-only, no control access).
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>My Nodes</h3>
          <input
            type="text"
            placeholder="Search nodes…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
            style={{
              padding: "4px 10px",
              borderRadius: 6,
              border: "1px solid var(--border)",
              background: "var(--bg-input)",
              color: "var(--text-primary)",
              fontSize: 12,
              width: 180,
            }}
          />
        </div>
        {(() => {
          const filtered = search
            ? nodes.filter((n) => ((n.name || n.node_id || "")).toLowerCase().includes(search.toLowerCase()))
            : nodes;
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
          return (
            <>
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
                    {paged.map((node) => {
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
                    {filtered.length === 0 && (
                      <tr>
                        <td colSpan={5} style={{ textAlign: "center", padding: 32 }}>
                          {search ? "No matching nodes" : "No nodes connected yet"}
                        </td>
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
