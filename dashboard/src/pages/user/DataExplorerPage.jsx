import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function DataExplorerPage() {
  const [archives, setArchives] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.archive()
      .then((data) => setArchives(Array.isArray(data) ? data : data.files || data.archives || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>Data Explorer</h1>
        <p>Browse and download raw detection logs and archive files</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Archive Files</div>
          <div className="stat-value">{archives.length}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Archived Detections</h3>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Filename</th>
                <th>Node</th>
                <th>Size</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {archives.map((file, i) => {
                const key = typeof file === "string" ? file : (file.key || "");
                const parts = key.split("/");
                const name = parts[parts.length - 1] || key;
                const node = parts[3] || parts[2] || extractNodeId(key) || "—";
                const size = file.size_bytes != null ? formatBytes(file.size_bytes) : "—";
                const date = file.modified ? new Date(file.modified).toLocaleString() : "—";
                return (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                    <td>{node}</td>
                    <td>{size}</td>
                    <td>{date}</td>
                  </tr>
                );
              })}
              {archives.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 32 }}>
                    No archived data available
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

function extractNodeId(filename) {
  const match = filename.match(/node[_-]?(\w+)/i);
  return match ? match[1].slice(0, 8) : "—";
}

function extractDate(filename) {
  const match = filename.match(/(\d{8}[-_]\d{6})/);
  return match ? match[1] : null;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
