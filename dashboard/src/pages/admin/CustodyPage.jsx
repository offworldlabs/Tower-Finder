import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function CustodyPage() {
  const [custody, setCustody] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.custody()
      .then(setCustody)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  // Custody data can be a dict of node chains or a single object
  const chains = custody?.chains || custody || {};
  const nodeIds = Object.keys(chains).filter((k) => k !== "total_chains" && k !== "status");

  return (
    <>
      <div className="page-header">
        <h1>Chain of Custody</h1>
        <p>Cryptographic verification and data integrity audit trail</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Active Chains</div>
          <div className="stat-value">{custody?.total_chains || nodeIds.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Verified</div>
          <div className="stat-value">
            {nodeIds.filter((id) => {
              const c = chains[id];
              return c?.verified || c?.valid;
            }).length}
          </div>
        </div>
      </div>

      {nodeIds.map((nodeId) => {
        const chain = chains[nodeId];
        if (!chain) return null;
        const entries = chain.entries || chain.chain || [];
        const isValid = chain.verified || chain.valid;

        return (
          <div className="card" key={nodeId} style={{ marginBottom: 16 }}>
            <div className="card-header">
              <h3 style={{ fontFamily: "monospace", fontSize: 13 }}>
                {nodeId.slice(-12)}
              </h3>
              <span className={`badge ${isValid ? "online" : "offline"}`}>
                {isValid ? "Verified" : "Unverified"}
              </span>
            </div>
            <div className="card-body">
              <table>
                <tbody>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Chain Length</td>
                    <td>{chain.length || entries.length || 0}</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Latest Hash</td>
                    <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {chain.latest_hash || chain.head || "—"}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>IQ Commitments</td>
                    <td>{chain.iq_commitments || 0}</td>
                  </tr>
                </tbody>
              </table>
              {entries.length > 0 && (
                <details style={{ marginTop: 12 }}>
                  <summary style={{ cursor: "pointer", color: "var(--accent)", fontSize: 13 }}>
                    View chain entries ({entries.length})
                  </summary>
                  <div className="config-block" style={{ marginTop: 8 }}>
                    {JSON.stringify(entries.slice(-5), null, 2)}
                  </div>
                </details>
              )}
            </div>
          </div>
        );
      })}

      {nodeIds.length === 0 && (
        <div className="card">
          <div className="card-body">
            <div className="empty-state">No chain of custody data available</div>
          </div>
        </div>
      )}
    </>
  );
}
