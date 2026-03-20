import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function StoragePage() {
  const [storage, setStorage] = useState(null);
  const [archives, setArchives] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.adminStorage(), api.archive()])
      .then(([s, a]) => {
        setStorage(s);
        setArchives(Array.isArray(a) ? a : a.files || a.archives || []);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>Data & Storage</h1>
        <p>Storage usage and data management</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Archive Files</div>
          <div className="stat-value">{storage?.archive_files || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Archive Size</div>
          <div className="stat-value">{storage?.archive_mb?.toFixed(1) || 0} MB</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Available Archives</div>
          <div className="stat-value">{archives.length}</div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><h3>Local Storage</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Archive Files</td>
                  <td>{storage?.archive_files || 0}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Total Size</td>
                  <td>{storage?.archive_mb?.toFixed(2) || 0} MB</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Raw Bytes</td>
                  <td>{(storage?.archive_bytes || 0).toLocaleString()}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><h3>B2 Cloud Storage</h3></div>
          <div className="card-body">
            <div className="empty-state">
              <p>B2 upload status and cost tracking coming soon</p>
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header"><h3>Recent Archives</h3></div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Filename</th>
                <th>Node</th>
                <th>Size</th>
              </tr>
            </thead>
            <tbody>
              {archives.slice(0, 50).map((file, i) => {
                const name = typeof file === "string" ? file : file.filename || file.name || "";
                return (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                    <td>{file.node_id || "—"}</td>
                    <td>{file.size ? formatBytes(file.size) : "—"}</td>
                  </tr>
                );
              })}
              {archives.length === 0 && (
                <tr>
                  <td colSpan={3} style={{ textAlign: "center", padding: 32 }}>No archives</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
