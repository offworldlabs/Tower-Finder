import { useState, useEffect } from "react";
import { api } from "../../api/client";

const PAGE_SIZE = 50;

export default function StoragePage() {
  const [storage, setStorage] = useState(null);
  const [archives, setArchives] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.adminStorage()
      .then((s) => setStorage(s))
      .catch(console.error);
  }, []);

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
        <h1>Data &amp; Storage</h1>
        <p>Storage usage and data management</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Archive Files</div>
          <div className="stat-value">{storage?.archive_files?.toLocaleString() ?? "—"}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Archive Size</div>
          <div className="stat-value">{storage?.archive_mb != null ? storage.archive_mb.toFixed(1) + " MB" : "—"}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Total Indexed</div>
          <div className="stat-value">{total.toLocaleString()}</div>
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
                  <td>{storage?.archive_files?.toLocaleString() ?? "—"}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Total Size</td>
                  <td>{storage?.archive_mb?.toFixed(2) ?? "0"} MB</td>
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
            {storage?.b2_status ? (
              <table>
                <tbody>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Status</td>
                    <td>
                      <span className={`badge ${storage.b2_status === "connected" ? "online" : "warning"}`}>
                        {storage.b2_status}
                      </span>
                    </td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Bucket</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{storage.b2_bucket || "—"}</td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <div className="empty-state">
                <p>B2 cloud storage not configured</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {storage?.per_node && Object.keys(storage.per_node).length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-header"><h3>Storage by Node</h3></div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Files</th>
                  <th>Size</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(storage.per_node).map(([nodeId, info]: [string, any]) => (
                  <tr key={nodeId}>
                    <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>{nodeId}</td>
                    <td>{(info.files || 0).toLocaleString()}</td>
                    <td>{formatBytes(info.bytes || 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Recent Archives</h3>
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
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={4} style={{ textAlign: "center", padding: 32 }}>Loading…</td></tr>
              ) : archives.length === 0 ? (
                <tr><td colSpan={4} style={{ textAlign: "center", padding: 32 }}>No archives</td></tr>
              ) : archives.map((file, i) => {
                const key = typeof file === "string" ? file : (file.key || "");
                const parts = key.split("/");
                const name = parts[parts.length - 1] || key;
                const node = parts.length >= 4 ? parts[3] : "—";
                const size = file.size_bytes != null ? formatBytes(file.size_bytes) : "—";
                const date = file.modified ? new Date(file.modified).toLocaleString() : "—";
                return (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{node}</td>
                    <td>{size}</td>
                    <td style={{ fontSize: 12 }}>{date}</td>
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
