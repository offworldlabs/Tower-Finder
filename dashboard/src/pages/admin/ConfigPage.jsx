import { useState, useEffect } from "react";
import { api } from "../../api/client";

const PAGE_SIZE = 25;

export default function ConfigPage() {
  const [nodeConfig, setNodeConfig] = useState(null);
  const [towerConfig, setTowerConfig] = useState(null);
  const [activeTab, setActiveTab] = useState("nodes");
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState([]);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");

  useEffect(() => {
    Promise.all([
      api.adminNodeConfig(),
      api.adminTowerConfig(),
      api.adminConfigHistory().catch(() => []),
    ])
      .then(([nc, tc, h]) => {
        setNodeConfig(nc);
        setTowerConfig(tc);
        setHistory(Array.isArray(h) ? h : h.versions || []);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const isLiveNodes = nodeConfig?._source === "live" && nodeConfig?.nodes;
  const isLiveTowers = towerConfig?._source === "live" && towerConfig?.towers;

  /* ── Nodes table data ── */
  const nodeEntries = isLiveNodes ? Object.entries(nodeConfig.nodes) : [];
  const filteredNodes = search
    ? nodeEntries.filter(([id]) => id.toLowerCase().includes(search.toLowerCase()))
    : nodeEntries;
  const nodeTotalPages = Math.ceil(filteredNodes.length / PAGE_SIZE);
  const pagedNodes = filteredNodes.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  /* ── Towers table data ── */
  const towerEntries = isLiveTowers ? Object.entries(towerConfig.towers) : [];
  const filteredTowers = search
    ? towerEntries.filter(([key, t]) =>
        key.includes(search) || (t.nodes_using || []).some((n) => n.toLowerCase().includes(search.toLowerCase()))
      )
    : towerEntries;
  const towerTotalPages = Math.ceil(filteredTowers.length / PAGE_SIZE);
  const pagedTowers = filteredTowers.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const showLiveTable = (activeTab === "nodes" && isLiveNodes && !editing) || (activeTab === "towers" && isLiveTowers && !editing);

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <>
      <div className="page-header">
        <h1>Configuration</h1>
        <p>View and manage node and tower configurations</p>
      </div>

      <div className="tabs">
        <button
          className={`tab ${activeTab === "nodes" ? "active" : ""}`}
          onClick={() => { setActiveTab("nodes"); setPage(0); setSearch(""); setEditing(false); }}
        >
          Node Config
        </button>
        <button
          className={`tab ${activeTab === "towers" ? "active" : ""}`}
          onClick={() => { setActiveTab("towers"); setPage(0); setSearch(""); setEditing(false); }}
        >
          Tower Config
        </button>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>
            {activeTab === "nodes"
              ? isLiveNodes ? `Live Node Config (${nodeConfig.total} nodes)` : "nodes_config.json"
              : isLiveTowers ? `Live Tower Config (${towerConfig.total} towers)` : "tower_config.json"}
          </h3>
          <div style={{ display: "flex", gap: 8 }}>
            {editing ? (
              <>
                <button
                  className="btn btn-primary btn-sm"
                  disabled={saving}
                  onClick={async () => {
                    try {
                      const parsed = JSON.parse(editText);
                      setSaving(true);
                      if (activeTab === "nodes") {
                        await api.adminUpdateNodeConfig(parsed);
                        setNodeConfig(parsed);
                      } else {
                        await api.adminUpdateTowerConfig(parsed);
                        setTowerConfig(parsed);
                      }
                      setEditing(false);
                    } catch (err) {
                      alert("Invalid JSON: " + err.message);
                    } finally {
                      setSaving(false);
                    }
                  }}
                >
                  {saving ? "Saving…" : "Save"}
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </>
            ) : (
              <button
                className="btn btn-outline btn-sm"
                onClick={() => {
                  const cfg = activeTab === "nodes" ? nodeConfig : towerConfig;
                  setEditText(JSON.stringify(cfg, null, 2));
                  setEditing(true);
                }}
              >
                Edit
              </button>
            )}
          </div>
        </div>
        <div className="card-body">
          {editing ? (
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              style={{
                width: "100%",
                minHeight: 400,
                fontFamily: "monospace",
                fontSize: 12,
                background: "var(--bg-input, #f8fafc)",
                color: "var(--text-primary)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                padding: 12,
                resize: "vertical",
              }}
            />
          ) : showLiveTable ? (
            <>
              <div style={{ marginBottom: 12 }}>
                <input
                  type="text"
                  placeholder="Search…"
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); setPage(0); }}
                  style={{ padding: "6px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, width: 260 }}
                />
              </div>

              {activeTab === "nodes" ? (
                <>
                  <div className="table-wrapper">
                    <table>
                      <thead>
                        <tr>
                          <th>Node ID</th>
                          <th>Status</th>
                          <th>RX Lat</th>
                          <th>RX Lon</th>
                          <th>TX Lat</th>
                          <th>TX Lon</th>
                          <th>Frequency</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pagedNodes.map(([id, n]) => (
                          <tr key={id}>
                            <td style={{ fontFamily: "monospace", fontSize: 12 }}>{id}</td>
                            <td><span className={`badge ${n.status === "active" ? "online" : "offline"}`}>{n.status || "—"}</span></td>
                            <td>{n.rx_lat != null ? n.rx_lat.toFixed(4) : "—"}</td>
                            <td>{n.rx_lon != null ? n.rx_lon.toFixed(4) : "—"}</td>
                            <td>{n.tx_lat != null ? n.tx_lat.toFixed(4) : "—"}</td>
                            <td>{n.tx_lon != null ? n.tx_lon.toFixed(4) : "—"}</td>
                            <td>{n.frequency || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {nodeTotalPages > 1 && (
                    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 12 }}>
                      <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
                      <span style={{ fontSize: 12 }}>Page {page + 1} of {nodeTotalPages}</span>
                      <button className="btn btn-sm" disabled={page >= nodeTotalPages - 1} onClick={() => setPage(page + 1)}>Next →</button>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="table-wrapper">
                    <table>
                      <thead>
                        <tr>
                          <th>Location</th>
                          <th>Lat</th>
                          <th>Lon</th>
                          <th>Frequency</th>
                          <th>Nodes Using</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pagedTowers.map(([key, t]) => (
                          <tr key={key}>
                            <td style={{ fontFamily: "monospace", fontSize: 12 }}>{key}</td>
                            <td>{t.lat?.toFixed(4)}</td>
                            <td>{t.lon?.toFixed(4)}</td>
                            <td>{t.frequency || "—"}</td>
                            <td style={{ fontSize: 11 }}>{(t.nodes_using || []).length}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {towerTotalPages > 1 && (
                    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 12 }}>
                      <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
                      <span style={{ fontSize: 12 }}>Page {page + 1} of {towerTotalPages}</span>
                      <button className="btn btn-sm" disabled={page >= towerTotalPages - 1} onClick={() => setPage(page + 1)}>Next →</button>
                    </div>
                  )}
                </>
              )}
            </>
          ) : (
            <div className="config-block">
              {JSON.stringify(
                activeTab === "nodes" ? nodeConfig : towerConfig,
                null,
                2
              )}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Version History</h3>
        </div>
        {history.length === 0 ? (
          <div className="card-body">
            <div className="empty-state">
              <p>No config changes recorded yet.</p>
            </div>
          </div>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>File</th>
                </tr>
              </thead>
              <tbody>
                {history.slice(0, 20).map((v, i) => (
                  <tr key={i}>
                    <td style={{ fontSize: 12 }}>{v.timestamp ? new Date(v.timestamp).toLocaleString() : v.file || "—"}</td>
                    <td>{v.type || "config"}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{v.file || v.name || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
