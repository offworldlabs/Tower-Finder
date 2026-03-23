import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function ConfigPage() {
  const [nodeConfig, setNodeConfig] = useState(null);
  const [towerConfig, setTowerConfig] = useState(null);
  const [activeTab, setActiveTab] = useState("nodes");
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState([]);

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

  return (
    <>
      <div className="page-header">
        <h1>Configuration</h1>
        <p>View and manage node and tower configurations</p>
      </div>

      <div className="tabs">
        <button
          className={`tab ${activeTab === "nodes" ? "active" : ""}`}
          onClick={() => setActiveTab("nodes")}
        >
          Node Config
        </button>
        <button
          className={`tab ${activeTab === "towers" ? "active" : ""}`}
          onClick={() => setActiveTab("towers")}
        >
          Tower Config
        </button>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>
            {activeTab === "nodes" ? "nodes_config.json" : "tower_config.json"}
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
