import { useAuth } from "../../context/AuthContext";

export default function SettingsPage() {
  const { user } = useAuth();

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
            <div className="empty-state">
              <p>Kit assignment and hardware information will be available once your node is provisioned.</p>
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Setup Guide</h3>
        </div>
        <div className="card-body">
          <div style={{ color: "var(--text-secondary)", fontSize: 14 }}>
            <p style={{ marginBottom: 12 }}>
              <strong>Getting Started with Your Retina Node</strong>
            </p>
            <ol style={{ paddingLeft: 20, display: "flex", flexDirection: "column", gap: 8 }}>
              <li>Connect your node hardware to power and ethernet</li>
              <li>The node will automatically register with the network</li>
              <li>Your node will appear in the Overview tab within minutes</li>
              <li>Monitor detections and trust score as the node calibrates</li>
              <li>Join the community Discord for support and updates</li>
            </ol>
          </div>
        </div>
      </div>
    </>
  );
}
