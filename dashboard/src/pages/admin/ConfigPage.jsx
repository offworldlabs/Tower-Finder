import { useState, useEffect } from "react";
import { api } from "../../api/client";

export default function ConfigPage() {
  const [nodeConfig, setNodeConfig] = useState(null);
  const [towerConfig, setTowerConfig] = useState(null);
  const [activeTab, setActiveTab] = useState("nodes");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.adminNodeConfig(), api.adminTowerConfig()])
      .then(([nc, tc]) => {
        setNodeConfig(nc);
        setTowerConfig(tc);
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
        </div>
        <div className="card-body">
          <div className="config-block">
            {JSON.stringify(
              activeTab === "nodes" ? nodeConfig : towerConfig,
              null,
              2
            )}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Config Management</h3>
        </div>
        <div className="card-body">
          <div className="empty-state">
            <p>Config push, compare, and version history coming soon.</p>
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
              Future: Push configs to nodes, track version history, and compare changes.
            </p>
          </div>
        </div>
      </div>
    </>
  );
}
