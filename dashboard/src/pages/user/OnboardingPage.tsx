import { useEffect, useState } from "react";
import { api } from "../../api/client";

type ClaimCode = {
  code: string;
  user_id: string;
  created_at: number;
  expires_at: number;
  used_at: number | null;
  used_by_node_id: string | null;
};

type OwnedNode = {
  node_id: string;
  name: string;
  status: string;
  last_heartbeat: string | null;
  is_synthetic: boolean;
  rx_lat: number | null;
  rx_lon: number | null;
  frequency: number | null;
};

export default function OnboardingPage() {
  const [codes, setCodes] = useState<ClaimCode[]>([]);
  const [nodes, setNodes] = useState<OwnedNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [c, n] = await Promise.all([api.myClaimCodes(), api.myNodes()]);
      setCodes(Array.isArray(c) ? c : []);
      setNodes(Array.isArray(n) ? n : []);
    } catch (e: any) {
      setError(e?.message || "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const generate = async () => {
    setBusy(true);
    setError(null);
    try {
      const rec = await api.createClaimCode();
      setCodes((prev) => [rec, ...prev]);
    } catch (e: any) {
      setError(e?.message || "Failed to generate code");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (code: string) => {
    if (!confirm(`Revoke code ${code}? It can no longer be used to claim a node.`)) return;
    try {
      await api.revokeClaimCode(code);
      setCodes((prev) => prev.filter((c) => c.code !== code));
    } catch (e: any) {
      setError(e?.message || "Failed to revoke");
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* noop */
    }
  };

  const activeCodes = codes.filter((c) => !c.used_at);
  const usedCodes = codes.filter((c) => c.used_at);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>Connect your node</h1>
        <p>Generate a claim code, flash it onto your radar node, and it will appear below once it connects.</p>
      </div>

      {error && (
        <div className="card" style={{ borderColor: "var(--accent-warning, #c0392b)" }}>
          <div className="card-body" style={{ color: "var(--accent-warning, #c0392b)" }}>{error}</div>
        </div>
      )}

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Owned Nodes</div>
          <div className="stat-value">{nodes.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Online Now</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status && n.status !== "disconnected" && n.status !== "never_connected").length}
          </div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Active Claim Codes</div>
          <div className="stat-value">{activeCodes.length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h3>Claim codes</h3>
          <button className="btn btn-primary btn-sm" onClick={generate} disabled={busy}>
            {busy ? "Generating…" : "Generate new code"}
          </button>
        </div>
        <div className="card-body">
          <p style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: 12 }}>
            Put the code in your node configuration as <code>claim_code</code> &mdash; it&rsquo;s sent in the HELLO message
            on first connect. Codes expire after 30 days and are single-use.
          </p>
          {codes.length === 0 ? (
            <div className="empty-state">No claim codes yet. Click &ldquo;Generate new code&rdquo; to start.</div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Expires</th>
                    <th>Bound to</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {codes.map((c) => {
                    const expired = !c.used_at && c.expires_at * 1000 < Date.now();
                    const status = c.used_at ? "used" : expired ? "expired" : "active";
                    return (
                      <tr key={c.code}>
                        <td>
                          <code style={{ fontSize: 14, letterSpacing: 1 }}>{c.code}</code>{" "}
                          {!c.used_at && !expired && (
                            <button
                              className="btn btn-outline btn-sm"
                              style={{ marginLeft: 6 }}
                              onClick={() => copy(c.code)}
                            >
                              Copy
                            </button>
                          )}
                        </td>
                        <td>
                          <span
                            className={`badge ${
                              status === "active" ? "online" : status === "used" ? "" : "warning"
                            }`}
                          >
                            {status}
                          </span>
                        </td>
                        <td>{new Date(c.created_at * 1000).toLocaleString()}</td>
                        <td>{new Date(c.expires_at * 1000).toLocaleDateString()}</td>
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>{c.used_by_node_id || "—"}</td>
                        <td>
                          {!c.used_at && !expired && (
                            <button className="btn btn-outline btn-sm" onClick={() => revoke(c.code)}>
                              Revoke
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                  {usedCodes.length === 0 && activeCodes.length === 0 && null}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>My nodes</h3>
        </div>
        <div className="card-body">
          {nodes.length === 0 ? (
            <div className="empty-state">
              No nodes claimed yet. Generate a claim code above and configure it on your hardware — once it
              connects to the server it will appear here.
            </div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Node ID</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Frequency</th>
                    <th>Location</th>
                    <th>Last heartbeat</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((n) => {
                    const online = n.status && n.status !== "disconnected" && n.status !== "never_connected";
                    return (
                      <tr key={n.node_id}>
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>{n.node_id}</td>
                        <td>{n.name}</td>
                        <td>
                          <span className={`badge ${online ? "online" : "offline"}`}>
                            {online ? "Online" : n.status === "never_connected" ? "Never connected" : "Offline"}
                          </span>
                        </td>
                        <td>{n.frequency ? `${(n.frequency / 1e6).toFixed(2)} MHz` : "—"}</td>
                        <td>
                          {n.rx_lat != null && n.rx_lon != null
                            ? `${n.rx_lat.toFixed(3)}, ${n.rx_lon.toFixed(3)}`
                            : "—"}
                        </td>
                        <td>{n.last_heartbeat ? new Date(n.last_heartbeat).toLocaleString() : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
