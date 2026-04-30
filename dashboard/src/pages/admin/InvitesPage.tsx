import { useEffect, useState } from "react";
import { api } from "../../api/client";

type Invite = {
  token: string;
  email: string;
  role: string;
  created_by: string;
  created_at: number;
  expires_at: number;
  used_at: number | null;
};

export default function InvitesPage() {
  const [invites, setInvites] = useState<Invite[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("user");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const data = await api.adminInvites();
      setInvites(Array.isArray(data) ? data : []);
    } catch (e: any) {
      setError(e?.message || "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api.adminCreateInvite(email.trim().toLowerCase(), role);
      setInvites((prev) => [inv, ...prev]);
      setEmail("");
      setRole("user");
    } catch (err: any) {
      setError(err?.message || "Failed to create invite");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (token: string, addr: string) => {
    if (!confirm(`Revoke invite for ${addr}?`)) return;
    try {
      await api.adminRevokeInvite(token);
      setInvites((prev) => prev.filter((i) => i.token !== token));
    } catch (err: any) {
      setError(err?.message || "Failed to revoke");
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  const pending = invites.filter((i) => !i.used_at && i.expires_at * 1000 > Date.now());
  const consumed = invites.filter((i) => i.used_at);

  return (
    <>
      <div className="page-header">
        <h1>Invites</h1>
        <p>Pre-approve users by email. When they sign in via Google or GitHub their role is applied automatically.</p>
      </div>

      {error && (
        <div className="card" style={{ borderColor: "var(--accent-warning, #c0392b)" }}>
          <div className="card-body" style={{ color: "var(--accent-warning, #c0392b)" }}>{error}</div>
        </div>
      )}

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Pending</div>
          <div className="stat-value">{pending.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Consumed</div>
          <div className="stat-value">{consumed.length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Create invite</h3>
        </div>
        <div className="card-body">
          <form onSubmit={create} style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 240px" }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)", display: "block", marginBottom: 4 }}>
                Email
              </label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="someone@example.com"
                style={{ width: "100%" }}
              />
            </div>
            <div>
              <label style={{ fontSize: 12, color: "var(--text-muted)", display: "block", marginBottom: 4 }}>
                Role
              </label>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="user">User</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button className="btn btn-primary" type="submit" disabled={busy || !email}>
              {busy ? "Inviting…" : "Send invite"}
            </button>
          </form>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>All invites</h3>
        </div>
        <div className="card-body">
          {invites.length === 0 ? (
            <div className="empty-state">No invites yet.</div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Expires</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {invites.map((i) => {
                    const expired = !i.used_at && i.expires_at * 1000 < Date.now();
                    const status = i.used_at ? "consumed" : expired ? "expired" : "pending";
                    return (
                      <tr key={i.token}>
                        <td>{i.email}</td>
                        <td style={{ textTransform: "capitalize" }}>
                          <span className={`badge ${i.role === "admin" ? "warning" : "online"}`}>{i.role}</span>
                        </td>
                        <td>
                          <span className={`badge ${status === "pending" ? "online" : status === "consumed" ? "" : "warning"}`}>
                            {status}
                          </span>
                        </td>
                        <td>{new Date(i.created_at * 1000).toLocaleString()}</td>
                        <td>{new Date(i.expires_at * 1000).toLocaleDateString()}</td>
                        <td>
                          {!i.used_at && !expired && (
                            <button className="btn btn-outline btn-sm" onClick={() => revoke(i.token, i.email)}>
                              Revoke
                            </button>
                          )}
                        </td>
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
