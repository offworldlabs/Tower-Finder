import { useState, useEffect, useCallback } from "react";
import "./PhysicsSettings.css";

const API = "/api";

// Object type definitions — colors match LiveAircraftMap rendering
const OBJECT_TYPES = [
  {
    key: "frac_anomalous",
    label: "Anomalous",
    countKey: "anomalous",
    color: "#f43f5e",
    border: "#e11d48",
    icon: "⚠",
    description: "Erratic flight — irregular altitude, speed, and heading changes.",
    maxPct: 30,
  },
  {
    key: "frac_drone",
    label: "Drone",
    countKey: "drone",
    color: "#f59e0b",
    border: "#d97706",
    icon: "✦",
    description: "Low-altitude, slow-moving quadrotor — no ADS-B transponder.",
    maxPct: 40,
  },
  {
    key: "frac_dark",
    label: "Dark Aircraft",
    countKey: "dark",
    color: "#94a3b8",
    border: "#64748b",
    icon: "●",
    description: "Commercial aircraft without ADS-B — radar-only detection.",
    maxPct: 50,
  },
];

function pct(v) { return Math.round(v * 100); }
function frac(p) { return Math.round(p) / 100; }

function LiveBadge({ count }) {
  if (count == null) return null;
  return (
    <span className="ps-live-badge">{count} live</span>
  );
}

export default function PhysicsSettings() {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);    // values being edited
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState(null);
  const [error, setError] = useState(null);

  // Fetch current config (includes ground_truth_counts)
  const fetchConfig = useCallback(async () => {
    try {
      const res = await fetch(`${API}/simulation/config`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setConfig(data);
      // Only reset draft on first load or if no pending edits
      setDraft(prev => prev ? prev : {
        frac_anomalous: data.frac_anomalous,
        frac_drone: data.frac_drone,
        frac_dark: data.frac_dark,
        max_range_km: data.max_range_km,
        min_aircraft: data.min_aircraft,
        max_aircraft: data.max_aircraft,
      });
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    fetchConfig();
    const id = setInterval(fetchConfig, 3000);
    return () => clearInterval(id);
  }, [fetchConfig]);

  async function handleApply() {
    setSaving(true);
    setSaveMsg(null);
    try {
      const res = await fetch(`${API}/simulation/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setSaveMsg("Applied — new objects will spawn with updated fractions.");
      setTimeout(() => setSaveMsg(null), 4000);
      await fetchConfig();
    } catch (e) {
      setSaveMsg(`Error: ${e.message}`);
    } finally {
      setSaving(false);
    }
  }

  function handleSlider(key, pctVal) {
    setDraft(prev => ({ ...prev, [key]: frac(pctVal) }));
  }

  if (!draft) {
    return (
      <div className="ps-container ps-loading">
        {error ? <span className="ps-error">{error}</span> : <span>Loading simulation config…</span>}
      </div>
    );
  }

  const counts = config?.ground_truth_counts ?? {};
  const totalGt = counts.total ?? 0;

  // Commercial = total - anomalous - drone - aircraft-type-dark
  // Note: in ground_truth_meta "aircraft" bucket = dark + commercial combined.
  // We surface the raw count and note it covers both.
  const fracCommercial = Math.max(0, 1 - draft.frac_anomalous - draft.frac_drone - draft.frac_dark);

  // Validate: sum must not exceed 1
  const fracSum = draft.frac_anomalous + draft.frac_drone + draft.frac_dark;
  const overLimit = fracSum > 1.0;

  return (
    <div className="ps-container">
      <div className="ps-header">
        <h2>Physics Object Layer</h2>
        <p className="ps-subtitle">
          Adjust the mix of synthetic aircraft types spawned by the fleet simulator.
          Changes take effect for newly spawned aircraft (every ~40 s).
        </p>
      </div>

      {/* Live count summary bar */}
      <div className="ps-counts-bar">
        <span className="ps-counts-label">Live objects</span>
        <span className="ps-count-chip" style={{ background: "#f43f5ebb" }}>
          ⚠ {counts.anomalous ?? 0} anomalous
        </span>
        <span className="ps-count-chip" style={{ background: "#f59e0bbb" }}>
          ✦ {counts.drone ?? 0} drone
        </span>
        <span className="ps-count-chip" style={{ background: "#64748bbb" }}>
          ● {counts.aircraft ?? 0} aircraft
        </span>
        <span className="ps-count-chip ps-count-total">
          {totalGt} total
        </span>
      </div>

      {/* Fraction sliders */}
      <div className="ps-sliders">
        {OBJECT_TYPES.map(({ key, label, countKey, color, icon, description, maxPct }) => (
          <div key={key} className="ps-slider-row">
            <div className="ps-type-label" style={{ borderLeftColor: color }}>
              <span className="ps-type-icon" style={{ color }}>{icon}</span>
              <div className="ps-type-text">
                <span className="ps-type-name">{label}</span>
                <span className="ps-type-desc">{description}</span>
              </div>
              <LiveBadge count={countKey === "dark" ? null : counts[countKey]} />
            </div>
            <div className="ps-slider-track">
              <input
                type="range"
                min={0}
                max={maxPct}
                step={1}
                value={pct(draft[key])}
                onChange={e => handleSlider(key, Number(e.target.value))}
                style={{ "--thumb-color": color }}
                className="ps-range"
              />
              <span className="ps-pct-label">{pct(draft[key])}%</span>
            </div>
          </div>
        ))}

        {/* Commercial (derived) */}
        <div className="ps-slider-row ps-commercial">
          <div className="ps-type-label" style={{ borderLeftColor: "#38bdf8" }}>
            <span className="ps-type-icon" style={{ color: "#38bdf8" }}>✈</span>
            <div className="ps-type-text">
              <span className="ps-type-name">Commercial</span>
              <span className="ps-type-desc">Standard aircraft with ADS-B transponder.</span>
            </div>
          </div>
          <div className="ps-slider-track">
            <div className="ps-derived-bar">
              <div
                className="ps-derived-fill"
                style={{ width: `${pct(fracCommercial)}%`, background: "#38bdf8" }}
              />
            </div>
            <span className="ps-pct-label" style={{ color: overLimit ? "#ef4444" : undefined }}>
              {overLimit ? "—" : `${pct(fracCommercial)}%`}
            </span>
          </div>
        </div>
      </div>

      {overLimit && (
        <div className="ps-warn">
          Sum of anomalous + drone + dark exceeds 100%. Reduce one slider before applying.
        </div>
      )}

      {/* Max range / aircraft count */}
      <div className="ps-misc">
        <label className="ps-misc-field">
          <span>Max detection range</span>
          <div className="ps-misc-input-row">
            <input
              type="range"
              min={40}
              max={300}
              step={10}
              value={draft.max_range_km}
              onChange={e => setDraft(prev => ({ ...prev, max_range_km: Number(e.target.value) }))}
              className="ps-range"
            />
            <span className="ps-pct-label">{draft.max_range_km} km</span>
          </div>
        </label>
        <label className="ps-misc-field">
          <span>Aircraft count window</span>
          <div className="ps-misc-input-row">
            <input
              type="number"
              min={1}
              max={500}
              value={draft.min_aircraft}
              onChange={e => setDraft(prev => ({ ...prev, min_aircraft: Number(e.target.value) }))}
              className="ps-number-input"
            />
            <span className="ps-misc-sep">–</span>
            <input
              type="number"
              min={1}
              max={500}
              value={draft.max_aircraft}
              onChange={e => setDraft(prev => ({ ...prev, max_aircraft: Number(e.target.value) }))}
              className="ps-number-input"
            />
          </div>
        </label>
      </div>

      <div className="ps-actions">
        <button
          className="ps-apply-btn"
          onClick={handleApply}
          disabled={saving || overLimit}
        >
          {saving ? "Applying…" : "Apply to Simulator"}
        </button>
        {saveMsg && (
          <span className={`ps-save-msg ${saveMsg.startsWith("Error") ? "ps-save-err" : ""}`}>
            {saveMsg}
          </span>
        )}
      </div>

      {/* Legend */}
      <div className="ps-legend">
        <div className="ps-legend-title">Map rendering key</div>
        <div className="ps-legend-items">
          <div className="ps-legend-item">
            <span className="ps-legend-dot" style={{ background: "#f43f5e" }} />
            <span>Anomalous — pulsing red ring on map</span>
          </div>
          <div className="ps-legend-item">
            <span className="ps-legend-dot ps-legend-drone" style={{ background: "#f59e0b" }} />
            <span>Drone — amber X-frame icon</span>
          </div>
          <div className="ps-legend-item">
            <span className="ps-legend-dot" style={{ background: "#94a3b8" }} />
            <span>Dark aircraft — grey bistatic arc only (no ADS-B)</span>
          </div>
          <div className="ps-legend-item">
            <span className="ps-legend-dot" style={{ background: "#38bdf8" }} />
            <span>Commercial — sky-blue aircraft icon with ADS-B</span>
          </div>
        </div>
      </div>
    </div>
  );
}
