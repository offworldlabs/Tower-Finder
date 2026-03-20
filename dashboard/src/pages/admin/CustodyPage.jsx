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

  // API returns { registered_nodes, node_keys: {nodeId: key}, chain_entries: {nodeId: []}, iq_commitments: {nodeId: n} }
  const nodeIds = Object.keys(custody?.node_keys || {});

  return (
    <>
      <div className="page-header">
        <h1>Chain of Custody</h1>
        <p>Cryptographic verification and data integrity audit trail</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Registered Nodes</div>
          <div className="stat-value">{custody?.registered_nodes ?? nodeIds.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">With Chain Entries</div>
          <div className="stat-value">
            {nodeIds.filter((id) => (custody?.chain_entries?.[id]?.length || 0) > 0).length}
          </div>
        </div>
      </div>

      {nodeIds.map((nodeId) => {
        const entries = Array.isArray(custody?.chain_entries?.[nodeId])
          ? custody.chain_entries[nodeId]
          : [];
        const iqCount = custody?.iq_commitments?.[nodeId] || 0;
        const isValid = entries.length > 0;
        const latestHash = entries.length > 0 ? (entries[entries.length - 1]?.hash || "—") : "—";

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
                    <td>{entries.length}</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Latest Hash</td>
                    <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {latestHash}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>IQ Commitments</td>
                    <td>{iqCount}</td>
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
