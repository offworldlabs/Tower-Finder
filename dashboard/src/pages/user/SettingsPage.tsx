import { useState, useEffect } from "react";
import { useAuth } from "../../context/AuthContext";
import { api } from "../../api/client";

export default function SettingsPage() {
  const { user } = useAuth();
  const [nodes, setNodes] = useState([]);

  useEffect(() => {
    api.myNodes()
      .then((n) => setNodes(Array.isArray(n) ? n : []))
      .catch(console.error);
  }, []);

  return (
    <>
      <div className="page-header">
        <h1>Settings</h1>
        <p>Account settings and preferences</p>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>Account Information</h3>
          </div>
          <div className="card-body">
            <table>
              <tbody>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Name</td>
                  <td>{user?.name}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Email</td>
                  <td>{user?.email}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Provider</td>
                  <td style={{ textTransform: "capitalize" }}>{user?.provider}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Role</td>
                  <td>
                    <span className={`badge ${user?.role === "admin" ? "warning" : "online"}`}>
                      {user?.role}
                    </span>
                  </td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Member Since</td>
                  <td>{user?.created_at ? new Date(user.created_at * 1000).toLocaleDateString() : "—"}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Kit & Hardware</h3>
          </div>
          <div className="card-body">
            {nodes.length === 0 ? (
              <div className="empty-state">
                <p>You don&rsquo;t own any nodes yet. Visit <a href="/onboarding">My Nodes</a> to generate a claim code.</p>
              </div>
            ) : (
              <table>
                <tbody>
                  {nodes.map((n) => (
                    <tr key={n.node_id}>
                      <td style={{ color: "var(--text-muted)" }}>Node</td>
                      <td style={{ fontFamily: "monospace", fontSize: 12 }}>{n.name || n.node_id}</td>
                      <td style={{ color: "var(--text-muted)" }}>Frequency</td>
                      <td>{n.frequency ? `${(n.frequency / 1e6).toFixed(1)} MHz` : "—"}</td>
                      <td style={{ color: "var(--text-muted)" }}>Status</td>
                      <td>
                        <span className={`badge ${n.status !== "disconnected" && n.status != null ? "online" : "offline"}`}>
                          {n.status !== "disconnected" && n.status != null ? "Online" : "Offline"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>


    </>
  );
}
