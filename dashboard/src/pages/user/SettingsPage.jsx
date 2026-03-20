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


    </>
  );
}
