import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function UserManagementPage() {
  const [users, setUsers] = useState([]);
  const [owners, setOwners] = useState<Record<string, { user_id: string }>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.adminUsers(), api.adminNodeOwners()])
      .then(([u, o]) => {
        setUsers(Array.isArray(u) ? u : []);
        setOwners(o && typeof o === "object" ? o : {});
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const nodeCount = (uid: string) =>
    Object.values(owners).filter((v) => v.user_id === uid).length;

  const toggleRole = async (user) => {
    const newRole = user.role === "admin" ? "user" : "admin";
    try {
      await api.adminSetRole(user.id, newRole);
      setUsers((prev) =>
        prev.map((u) => (u.id === user.id ? { ...u, role: newRole } : u))
      );
    } catch (err) {
      console.error("Failed to update role:", err);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>User Management</h1>
        <p>Manage operators and admin access</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Users</div>
          <div className="stat-value">{users.length}</div>
        </div>
        <div className="stat-card warning">
          <div className="stat-label">Admins</div>
          <div className="stat-value">{users.filter((u) => u.role === "admin").length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Registered Users</h3>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Email</th>
                <th>Provider</th>
                <th>Role</th>
                <th>Nodes</th>
                <th>Last Login</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    {user.avatar ? (
                      <img
                        src={user.avatar}
                        alt=""
                        style={{ width: 24, height: 24, borderRadius: "50%" }}
                        referrerPolicy="no-referrer"
                      />
                    ) : null}
                    <span style={{ color: "var(--text-primary)" }}>{user.name}</span>
                  </td>
                  <td>{user.email}</td>
                  <td style={{ textTransform: "capitalize" }}>{user.provider}</td>
                  <td>
                    <span className={`badge ${user.role === "admin" ? "warning" : "online"}`}>
                      {user.role}
                    </span>
                  </td>
                  <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--text-muted)" }}>
                    {nodeCount(user.id)}
                  </td>
                  <td>
                    {user.last_login
                      ? new Date(user.last_login * 1000).toLocaleString()
                      : "—"}
                  </td>
                  <td>
                    <button className="btn btn-outline btn-sm" onClick={() => toggleRole(user)}>
                      {user.role === "admin" ? "Demote" : "Promote"}
                    </button>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: "center", padding: 32 }}>
                    No users registered yet
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
