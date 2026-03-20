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

  // API returns { registered_nodes, node_keys: {nodeId: {fingerprint, signing_mode, ...}}, chain_entries: {nodeId: {count, latest_hour, latest_verified}}, iq_commitments: {nodeId: count} }
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
            {nodeIds.filter((id) => (custody?.chain_entries?.[id]?.count || 0) > 0).length}
          </div>
        </div>
      </div>

      {nodeIds.map((nodeId) => {
        // chain_entries[nodeId] = {count, latest_hour, latest_verified} (summary from API)
        const chainSummary = custody?.chain_entries?.[nodeId] || {};
        const chainCount = chainSummary.count || 0;
        const isValid = chainSummary.latest_verified === true;
        const latestHour = chainSummary.latest_hour || "—";
        const iqCount = custody?.iq_commitments?.[nodeId] || 0;
        const keyInfo = custody?.node_keys?.[nodeId] || {};

        return (
          <div className="card" key={nodeId} style={{ marginBottom: 16 }}>
            <div className="card-header">
              <h3 style={{ fontFamily: "monospace", fontSize: 13 }}>
                {nodeId.slice(-12)}
              </h3>
              <span className={`badge ${isValid ? "online" : chainCount > 0 ? "warning" : "offline"}`}>
                {isValid ? "Verified" : chainCount > 0 ? "Unverified" : "No Entries"}
              </span>
            </div>
            <div className="card-body">
              <table>
                <tbody>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Chain Length</td>
                    <td>{chainCount}</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Latest Hour (UTC)</td>
                    <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {latestHour}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>IQ Commitments</td>
                    <td>{iqCount}</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Signing Mode</td>
                    <td>{keyInfo.signing_mode || "—"}</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Key Fingerprint</td>
                    <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {keyInfo.fingerprint || "—"}
                    </td>
                  </tr>
                </tbody>
              </table>
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
