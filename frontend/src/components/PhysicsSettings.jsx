import { useState, useEffect, useCallback } from "react";
import { MapContainer, TileLayer, CircleMarker, Tooltip } from "react-leaflet";
import "./PhysicsSettings.css";

const API = "/api";

// Same plane path used by LiveAircraftMap for pixel-perfect icon matching
const PLANE_PATH =
  "M16,2 C15.3,5.5 14.7,9 14.7,13 L3,20 L3,23 L14.7,19 L14.7,26 L11.5,28 L11.5,30.5 L16,29 L20.5,30.5 L20.5,28 L17.3,26 L17.3,19 L29,23 L29,20 L17.3,13 C17.3,9 16.7,5.5 16,2Z";

// SVG icon components — colours match map rendering exactly
function PlaneIcon({ color = "#38bdf8", size = 26 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" style={{ display: "block", filter: `drop-shadow(0 0 4px ${color}55)` }}>
      <path d={PLANE_PATH} fill={color} />
    </svg>
  );
}

function DarkPlaneIcon({ size = 26 }) {
  // Grey plane with a diagonal "no-signal" bar — conveys ADS-B-off
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" style={{ display: "block" }}>
      <path d={PLANE_PATH} fill="#94a3b8" opacity="0.5" />
      <line x1="6" y1="6" x2="26" y2="26" stroke="#64748b" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

function DroneIcon({ size = 26 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: "block", filter: "drop-shadow(0 0 4px rgba(245,158,11,0.4))" }}>
      <line x1="4" y1="4" x2="20" y2="20" stroke="#f59e0b" strokeWidth="2.2" strokeLinecap="round" />
      <line x1="20" y1="4" x2="4" y2="20" stroke="#f59e0b" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="4"  cy="4"  r="3" fill="none" stroke="#f59e0b" strokeWidth="1.5" />
      <circle cx="20" cy="4"  r="3" fill="none" stroke="#f59e0b" strokeWidth="1.5" />
      <circle cx="4"  cy="20" r="3" fill="none" stroke="#f59e0b" strokeWidth="1.5" />
      <circle cx="20" cy="20" r="3" fill="none" stroke="#f59e0b" strokeWidth="1.5" />
      <circle cx="12" cy="12" r="2.5" fill="#f59e0b" />
    </svg>
  );
}

function AnomalousIcon({ size = 26 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: "block", filter: "drop-shadow(0 0 4px rgba(244,63,94,0.5))" }}>
      <circle cx="12" cy="12" r="9"  fill="none" stroke="#f43f5e" strokeWidth="1.5" strokeDasharray="4 2" />
      <circle cx="12" cy="12" r="5.5" fill="none" stroke="#f43f5e" strokeWidth="1" opacity="0.5" />
      <line  x1="12" y1="7.5" x2="12" y2="13.5" stroke="#f43f5e" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="12" cy="16" r="1" fill="#f43f5e" />
    </svg>
  );
}

// Object type definitions — colours and SVG icons match LiveAircraftMap rendering precisely
const OBJECT_TYPES = [
  {
    key: "frac_anomalous",
    label: "Anomalous",
    countKey: "anomalous",
    color: "#f43f5e",
    Icon: AnomalousIcon,
    description: "Erratic flight — irregular altitude, speed, and heading changes.",
    mapNote: "Pulsing red ring on map",
    maxPct: 30,
  },
  {
    key: "frac_drone",
    label: "Drone",
    countKey: "drone",
    color: "#f59e0b",
    Icon: DroneIcon,
    description: "Low-altitude, slow-moving quadrotor — no ADS-B transponder.",
    mapNote: "Amber X-frame icon on map",
    maxPct: 40,
  },
  {
    key: "frac_dark",
    label: "Dark Aircraft",
    countKey: "dark",
    color: "#94a3b8",
    Icon: DarkPlaneIcon,
    description: "Commercial aircraft without ADS-B — radar-only detection.",
    mapNote: "Grey bistatic arc only (no ADS-B icon)",
    maxPct: 50,
  },
];

function pct(v) { return Math.round(v * 100); }
function frac(p) { return Math.round(p) / 100; }

export default function PhysicsSettings() {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState(null);
  const [error, setError] = useState(null);
  const [gtData, setGtData] = useState(null);

  const fetchConfig = useCallback(async () => {
    try {
      const res = await fetch(`${API}/simulation/config`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setConfig(data);
      setDraft(prev => prev ? prev : {
        frac_anomalous: data.frac_anomalous,
        frac_drone:     data.frac_drone,
        frac_dark:      data.frac_dark,
        max_range_km:   data.max_range_km,
        min_aircraft:   data.min_aircraft,
        max_aircraft:   data.max_aircraft,
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

  const fetchGt = useCallback(async () => {
    try {
      const res = await fetch(`${API}/simulation/ground-truth`);
      if (!res.ok) return;
      const data = await res.json();
      setGtData(data);
    } catch {
      // non-fatal — GT section just stays empty
    }
  }, []);

  useEffect(() => {
    fetchGt();
    const id = setInterval(fetchGt, 5000);
    return () => clearInterval(id);
  }, [fetchGt]);

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
        {error
          ? <span className="ps-error">{error}</span>
          : <span className="ps-loading-text">Loading simulation config…</span>}
      </div>
    );
  }

  const counts        = config?.ground_truth_counts ?? {};
  const totalGt       = counts.total ?? 0;
  const fracCommercial = Math.max(0, 1 - draft.frac_anomalous - draft.frac_drone - draft.frac_dark);
  const fracSum        = draft.frac_anomalous + draft.frac_drone + draft.frac_dark;
  const overLimit      = fracSum > 1.0;

  return (
    <div className="ps-container">

      {/* ── Header ──────────────────────────────────────────────────── */}
      <div className="ps-header">
        <h2>Physics Object Layer</h2>
        <p className="ps-subtitle">
          Adjust the mix of synthetic aircraft types spawned by the fleet simulator.
          Changes apply to newly-spawned objects (next spawn cycle ~40 s).
        </p>
      </div>

      {/* ── Live Count Grid ──────────────────────────────────────────── */}
      <div className="ps-count-grid">
        <div className="ps-count-card" style={{ "--accent": "#f43f5e" }}>
          <AnomalousIcon size={28} />
          <span className="ps-count-num">{counts.anomalous ?? 0}</span>
          <span className="ps-count-lbl">Anomalous</span>
        </div>
        <div className="ps-count-card" style={{ "--accent": "#f59e0b" }}>
          <DroneIcon size={28} />
          <span className="ps-count-num">{counts.drone ?? 0}</span>
          <span className="ps-count-lbl">Drones</span>
        </div>
        <div className="ps-count-card" style={{ "--accent": "#94a3b8" }}>
          <PlaneIcon color="#94a3b8" size={28} />
          <span className="ps-count-num">{counts.aircraft ?? 0}</span>
          <span className="ps-count-lbl">Aircraft</span>
        </div>
        <div className="ps-count-card ps-count-total" style={{ "--accent": "#38bdf8" }}>
          <PlaneIcon color="#38bdf8" size={28} />
          <span className="ps-count-num">{totalGt}</span>
          <span className="ps-count-lbl">Total Live</span>
        </div>
      </div>

      {/* ── Composition Bar ─────────────────────────────────────────── */}
      <div className="ps-comp-section">
        <div className="ps-comp-label">Fleet composition</div>
        <div className="ps-comp-bar">
          {draft.frac_anomalous > 0 && (
            <div className="ps-comp-seg" style={{ flex: draft.frac_anomalous, background: "#f43f5e" }} />
          )}
          {draft.frac_drone > 0 && (
            <div className="ps-comp-seg" style={{ flex: draft.frac_drone, background: "#f59e0b" }} />
          )}
          {draft.frac_dark > 0 && (
            <div className="ps-comp-seg" style={{ flex: draft.frac_dark, background: "#64748b" }} />
          )}
          {fracCommercial > 0 && (
            <div className="ps-comp-seg" style={{ flex: fracCommercial, background: "#38bdf8" }} />
          )}
        </div>
        <div className="ps-comp-legend">
          <span style={{ color: "#f43f5e" }}>■ {pct(draft.frac_anomalous)}% anomalous</span>
          <span style={{ color: "#f59e0b" }}>■ {pct(draft.frac_drone)}% drone</span>
          <span style={{ color: "#64748b" }}>■ {pct(draft.frac_dark)}% dark</span>
          <span style={{ color: "#38bdf8" }}>■ {pct(fracCommercial)}% commercial</span>
        </div>
      </div>

      {/* ── Type Sliders ────────────────────────────────────────────── */}
      <div className="ps-sliders">
        {OBJECT_TYPES.map(({ key, label, countKey, color, Icon, description, mapNote, maxPct }) => {
          const fillPct = (pct(draft[key]) / maxPct) * 100;
          return (
            <div key={key} className="ps-type-card" style={{ "--accent": color }}>
              <div className="ps-type-header">
                <div className="ps-type-icon-wrap">
                  <Icon size={24} />
                </div>
                <div className="ps-type-meta">
                  <span className="ps-type-name">{label}</span>
                  <span className="ps-type-note">{mapNote}</span>
                </div>
                <div className="ps-type-badge" style={{ background: color + "22", color }}>
                  {counts[countKey] ?? 0} live
                </div>
              </div>
              <p className="ps-type-desc">{description}</p>
              <div className="ps-slider-row">
                <input
                  type="range"
                  min={0}
                  max={maxPct}
                  step={1}
                  value={pct(draft[key])}
                  onChange={e => handleSlider(key, Number(e.target.value))}
                  className="ps-range"
                  style={{
                    "--thumb-color": color,
                    "--fill-pct":    `${fillPct}%`,
                  }}
                />
                <span className="ps-pct-val" style={{ color }}>{pct(draft[key])}%</span>
              </div>
            </div>
          );
        })}

        {/* Commercial — derived, read-only */}
        <div className="ps-type-card ps-commercial" style={{ "--accent": "#38bdf8" }}>
          <div className="ps-type-header">
            <div className="ps-type-icon-wrap">
              <PlaneIcon color="#38bdf8" size={24} />
            </div>
            <div className="ps-type-meta">
              <span className="ps-type-name">Commercial</span>
              <span className="ps-type-note">Sky-blue aircraft icon · ADS-B transponder</span>
            </div>
            <div className="ps-type-badge" style={{ background: "#38bdf822", color: "#38bdf8" }}>
              derived
            </div>
          </div>
          <p className="ps-type-desc">Standard aircraft with ADS-B. Remainder of fleet after anomalous + drone + dark.</p>
          <div className="ps-slider-row">
            <div className="ps-derived-track">
              <div
                className="ps-derived-fill"
                style={{
                  width:      `${overLimit ? 0 : pct(fracCommercial)}%`,
                  background: "#38bdf8",
                }}
              />
            </div>
            <span className="ps-pct-val" style={{ color: overLimit ? "#ef4444" : "#38bdf8" }}>
              {overLimit ? "—" : `${pct(fracCommercial)}%`}
            </span>
          </div>
        </div>
      </div>

      {overLimit && (
        <div className="ps-warn">
          Anomalous + drone + dark exceeds 100%. Reduce a slider before applying.
        </div>
      )}

      {/* ── Settings ────────────────────────────────────────────────── */}
      <div className="ps-settings-grid">
        <div className="ps-settings-card">
          <div className="ps-settings-label">Max detection range</div>
          <div className="ps-slider-row">
            <input
              type="range"
              min={40}
              max={300}
              step={10}
              value={draft.max_range_km}
              onChange={e => setDraft(prev => ({ ...prev, max_range_km: Number(e.target.value) }))}
              className="ps-range"
              style={{
                "--thumb-color": "#38bdf8",
                "--fill-pct":    `${((draft.max_range_km - 40) / 260) * 100}%`,
              }}
            />
            <span className="ps-pct-val" style={{ color: "#38bdf8", minWidth: "4.5rem" }}>
              {draft.max_range_km} km
            </span>
          </div>
        </div>

        <div className="ps-settings-card">
          <div className="ps-settings-label">Aircraft count window</div>
          <div className="ps-count-inputs">
            <div className="ps-count-field">
              <span className="ps-count-field-lbl">Min</span>
              <input
                type="number"
                min={1}
                max={500}
                value={draft.min_aircraft}
                onChange={e => setDraft(prev => ({ ...prev, min_aircraft: Number(e.target.value) }))}
                className="ps-number-input"
              />
            </div>
            <span className="ps-count-sep">—</span>
            <div className="ps-count-field">
              <span className="ps-count-field-lbl">Max</span>
              <input
                type="number"
                min={1}
                max={500}
                value={draft.max_aircraft}
                onChange={e => setDraft(prev => ({ ...prev, max_aircraft: Number(e.target.value) }))}
                className="ps-number-input"
              />
            </div>
          </div>
        </div>
      </div>

      {/* ── Apply ───────────────────────────────────────────────────── */}
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

      {/* ── Ground Truth Map ────────────────────────────────────────── */}
      {gtData && gtData.aircraft.length > 0 && (() => {
        const ac = gtData.aircraft;
        const centerLat = ac.reduce((s, a) => s + a.lat, 0) / ac.length;
        const centerLon = ac.reduce((s, a) => s + a.lon, 0) / ac.length;

        function acColor(a) {
          if (a.is_anomalous)           return "#f43f5e";
          if (a.object_type === "drone") return "#f59e0b";
          if (a.object_type === "dark")  return "#64748b";
          return "#38bdf8";
        }

        return (
          <div className="ps-gt-section">
            <div className="ps-gt-header">
              <span className="ps-gt-title">Ground Truth Map</span>
              <span className="ps-gt-count">{ac.length} objects</span>
            </div>
            <div className="ps-gt-map">
              <MapContainer
                center={[centerLat, centerLon]}
                zoom={6}
                style={{ width: "100%", height: "100%" }}
                zoomControl={false}
                attributionControl={false}
              >
                <TileLayer url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png" />
                {ac.map(a => (
                  <CircleMarker
                    key={a.hex}
                    center={[a.lat, a.lon]}
                    radius={a.object_type === "drone" ? 4 : 5}
                    pathOptions={{
                      color:       acColor(a),
                      fillColor:   acColor(a),
                      fillOpacity: 0.85,
                      weight:      a.is_anomalous ? 2 : 1,
                    }}
                  >
                    <Tooltip>{a.object_type}{a.is_anomalous ? " ⚠ anomalous" : ""} · {Math.round(a.alt_m)} m</Tooltip>
                  </CircleMarker>
                ))}
              </MapContainer>
            </div>
            <div className="ps-gt-legend">
              <span style={{ color: "#38bdf8" }}>● Commercial</span>
              <span style={{ color: "#94a3b8" }}>● Dark</span>
              <span style={{ color: "#f59e0b" }}>● Drone</span>
              <span style={{ color: "#f43f5e" }}>● Anomalous</span>
            </div>
          </div>
        );
      })()}

      {/* ── Solver Performance ──────────────────────────────────────── */}
      {gtData?.performance && (
        <div className="ps-perf-section">
          <div className="ps-perf-title">Solver Performance</div>
          <div className="ps-perf-grid">
            <div className="ps-perf-card">
              <span className="ps-perf-val">{gtData.performance.gt_total}</span>
              <span className="ps-perf-lbl">GT Objects</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">{gtData.performance.detected}</span>
              <span className="ps-perf-lbl">Detected</span>
            </div>
            <div className="ps-perf-card ps-perf-rate">
              <span className="ps-perf-val">{gtData.performance.detection_rate_pct}%</span>
              <span className="ps-perf-lbl">Detection Rate</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">
                {gtData.performance.avg_position_error_km != null
                  ? `${gtData.performance.avg_position_error_km} km`
                  : "—"}
              </span>
              <span className="ps-perf-lbl">Avg Position Error</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">{gtData.performance.multinode_tracks}</span>
              <span className="ps-perf-lbl">Multinode Tracks</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">{gtData.performance.tracked_with_error}</span>
              <span className="ps-perf-lbl">Positions Compared</span>
            </div>
          </div>
        </div>
      )}

      {/* ── Doppler Arc Guide ───────────────────────────────────────── */}
      <div className="ps-doppler-guide">
        <div className="ps-doppler-title">
          <svg width="14" height="14" viewBox="0 0 24 24" style={{ display:"inline-block", verticalAlign:"middle", marginRight:6 }}>
            <path d="M12 2 Q20 12 12 22 Q4 12 12 2Z" fill="none" stroke="#60a5fa" strokeWidth="1.5" />
            <ellipse cx="12" cy="12" rx="4" ry="8" fill="none" stroke="#60a5fa" strokeWidth="1" opacity="0.5" />
          </svg>
          Doppler Bistatic Arcs
        </div>
        <p className="ps-doppler-body">
          Single-node aircraft (detected by only one radar node) render a
          coloured bistatic arc on the Live Radar map instead of a position fix.
          The arc colour encodes the Doppler shift measured by that node.
        </p>
        <div className="ps-doppler-gradient-row">
          <span className="ps-doppler-end">← Approaching</span>
          <div className="ps-doppler-bar" />
          <span className="ps-doppler-end">Receding →</span>
        </div>
        <p className="ps-doppler-hint">
          <strong>Where to find them:</strong> Live Radar tab → zoom into SE United States (Florida / Gulf Coast)
          → look for curved coloured lines instead of aircraft icons. Solo nodes there produce the most arcs.
        </p>
      </div>

    </div>
  );
}
