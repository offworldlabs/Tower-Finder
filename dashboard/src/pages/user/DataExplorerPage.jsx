import { useState, useEffect } from "react";
import { api, downloadUrl } from "../../api/client";

const PAGE_SIZE = 50;

export default function DataExplorerPage() {
  const [archives, setArchives] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.archive(PAGE_SIZE, page * PAGE_SIZE)
      .then((data) => {
        setArchives(data.files || []);
        setTotal(data.total ?? data.count ?? 0);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [page]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <div className="page-header">
        <h1>Data Explorer</h1>
        <p>Browse and download raw detection logs and archive files</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Archive Files</div>
          <div className="stat-value">{total.toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Showing Page</div>
          <div className="stat-value">{page + 1} / {totalPages}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Archived Detections</h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              className="btn btn-secondary"
              disabled={page === 0 || loading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              ← Prev
            </button>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total.toLocaleString()}
            </span>
            <button
              className="btn btn-secondary"
              disabled={page >= totalPages - 1 || loading}
              onClick={() => setPage((p) => p + 1)}
            >
              Next →
            </button>
          </div>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Filename</th>
                <th>Node</th>
                <th>Size</th>
                <th>Date</th>
                <th>Download</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", padding: 32 }}>Loading…</td>
                </tr>
              ) : archives.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", padding: 32 }}>No archived data available</td>
                </tr>
              ) : archives.map((file, i) => {
                const key = typeof file === "string" ? file : (file.key || "");
                const parts = key.split("/");
                const name = parts[parts.length - 1] || key;
                // key structure: YYYY/MM/DD/node_id/filename.json
                const node = parts.length >= 4 ? parts[3] : (parts[2] || "—");
                const size = file.size_bytes != null ? formatBytes(file.size_bytes) : "—";
                const date = file.modified ? new Date(file.modified).toLocaleString() : "—";
                return (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{node}</td>
                    <td>{size}</td>
                    <td style={{ fontSize: 12 }}>{date}</td>
                    <td>
                      <a
                        href={downloadUrl(`/api/data/archive/${encodeURIComponent(key)}`)}
                        className="btn btn-outline btn-sm"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Download
                      </a>
                    </td>
                  </tr>
                );
              })}
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
