import { useState, useEffect, useCallback, useRef } from "react";
import { MapContainer, TileLayer, CircleMarker, Tooltip } from "react-leaflet";
import "./PhysicsSettings.css";

// One plane path — imported from the icon module the map itself uses.
import { PLANE_PATH } from "./map/icons";
import {
  formatKm,
  formatPct,
  funnelSegments,
  rejectReasonBars,
  consensusModeLabel,
  claimModeLabel,
} from "./physics/solverReport";

const API = "/api";

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

// min/max_aircraft and the scene keys are only-if-set on the backend (a fresh
// backend ships none of them — see core/state.py), so a sensible default fills
// in until an operator PUTs one. Shared by the initial seed and the drift
// re-sync so both paths can never disagree about what "the server says".
function serverToDraft(data) {
  return {
    frac_anomalous: data.frac_anomalous,
    frac_drone:     data.frac_drone,
    frac_dark:      data.frac_dark,
    min_aircraft:   data.min_aircraft ?? 20,
    max_aircraft:   data.max_aircraft ?? 40,
  };
}

function serverToScene(data) {
  return {
    n_nodes:       data.n_nodes ?? 30,
    dual_fraction: data.dual_fraction ?? 0.0,
    max_range_km:  data.max_range_km ?? 0,
  };
}

export default function PhysicsSettings() {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState(null);
  const [error, setError] = useState(null);
  const [gtData, setGtData] = useState(null);

  // Fleet Scene — node count / dual fraction. Deliberately separate from
  // `draft` above: merging it in would restart the fleet simulator on every
  // frac_dark/frac_drone tweak, which only needs an in-process reapply.
  const [sceneDraft, setSceneDraft] = useState(null);
  const [sceneApplying, setSceneApplying] = useState(false);
  const [sceneArmed, setSceneArmed] = useState(false);
  const [sceneMsg, setSceneMsg] = useState(null);
  const [scenePending, setScenePending] = useState(false);
  const [runningScene, setRunningScene] = useState(null);
  const sceneArmTimerRef = useRef(null);
  useEffect(() => {
    return () => { if (sceneArmTimerRef.current) clearTimeout(sceneArmTimerRef.current); };
  }, []);

  // Dead-reckoning animation state for the GT map
  const fixesRef = useRef({});
  const [animatedAircraft, setAnimatedAircraft] = useState([]);

  // ── Drift detection ────────────────────────────────────────────────────
  // `_updated_at` changes on every accepted PUT and is re-stamped when the
  // backend imports core/state.py, so a value this page did not write means
  // the config moved underneath it — nearly always a backend restart landing
  // on boot state. Without this the drafts seeded once and never re-synced,
  // so the sliders kept showing an applied scene while the simulator had
  // already been pushed back to defaults. Compared, never displayed.
  const lastStampRef  = useRef(null);
  const seededRef     = useRef(false);
  const dirtyRef      = useRef(false);
  const sceneDirtyRef = useRef(false);
  const [drift, setDrift] = useState(null);

  // Aborted on unmount — in-flight responses used to resolve into setState
  // on an unmounted component.
  const abortRef = useRef(null);
  useEffect(() => {
    abortRef.current = new AbortController();
    return () => abortRef.current?.abort();
  }, []);

  const fetchConfig = useCallback(async () => {
    try {
      const res = await fetch(`${API}/simulation/config`, { signal: abortRef.current?.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (abortRef.current?.signal.aborted) return;
      setConfig(data);

      const stamp = typeof data._updated_at === "number" ? data._updated_at : null;
      const prevStamp = lastStampRef.current;
      lastStampRef.current = stamp;

      // First payload seeds both drafts. sceneDraft is seeded here too but is
      // NEVER merged into `draft` — the two PUT different payloads.
      if (!seededRef.current) {
        seededRef.current = true;
        setDraft(serverToDraft(data));
        setSceneDraft(serverToScene(data));
        return;
      }

      // Nothing to compare against on the first stamp we see, and a payload
      // without one (older backend) can't be reasoned about at all.
      if (prevStamp === null || stamp === null || stamp === prevStamp) return;

      // A restart reverting to boot state usually moves the stamp *backwards*
      // (import time of the new process vs the operator's later PUT), but a
      // long-running backend restarted after a stale snapshot can move it
      // forwards — either way it is a change this page did not make.
      const reverted = stamp < prevStamp;
      if (dirtyRef.current || sceneDirtyRef.current) {
        // Unapplied edits belong to the operator — never overwrite them.
        // Say the baseline moved and let them apply or discard.
        setDrift({ kind: "stale", reverted });
      } else {
        setDraft(serverToDraft(data));
        setSceneDraft(serverToScene(data));
        setDrift({ kind: "resynced", reverted });
      }
    } catch (e) {
      setError(e.message);
    }
  }, []);

  // Discard unapplied edits and take whatever the simulator is running.
  // Clearing seededRef makes the next payload seed instead of compare, so
  // there is no window where the drafts and the stamp disagree.
  const reloadFromSimulator = useCallback(async () => {
    dirtyRef.current = false;
    sceneDirtyRef.current = false;
    seededRef.current = false;
    lastStampRef.current = null;
    setDrift(null);
    await fetchConfig();
  }, [fetchConfig]);

  useEffect(() => {
    fetchConfig();
    const id = setInterval(fetchConfig, 10000);
    return () => clearInterval(id);
  }, [fetchConfig]);

  const fetchGt = useCallback(async () => {
    try {
      const res = await fetch(`${API}/simulation/ground-truth`, { signal: abortRef.current?.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (abortRef.current?.signal.aborted) return;
      // Store fixes for dead-reckoning
      const newFixes = {};
      for (const ac of data.aircraft) {
        newFixes[ac.hex] = { ...ac };
      }
      fixesRef.current = newFixes;
      setGtData(data);
    } catch {
      // non-fatal — GT section just stays empty
    }
  }, []);

  useEffect(() => {
    fetchGt();
    const id = setInterval(fetchGt, 2000);
    return () => clearInterval(id);
  }, [fetchGt]);

  // Solver Report — funnel/error/ghost/consensus stats. Separate, slower
  // poll from fetchGt: it scans up to 8000 mlat_solve_history records, so a
  // 2 Hz cadence would be wasted work for numbers that only move on solves.
  const [solverStats, setSolverStats] = useState(null);
  const fetchSolverStats = useCallback(async () => {
    try {
      const res = await fetch(`${API}/test/solver-stats?minutes=10`, { signal: abortRef.current?.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (abortRef.current?.signal.aborted) return;
      setSolverStats(data);
    } catch {
      // non-fatal — Solver Report just holds the last good data
    }
  }, []);

  useEffect(() => {
    fetchSolverStats();
    const id = setInterval(fetchSolverStats, 5000);
    return () => clearInterval(id);
  }, [fetchSolverStats]);

  // Dead-reckoning animation — smooth position updates at 500 ms
  useEffect(() => {
    const KNOTS_TO_MS = 0.514444;
    const DEG_PER_M = 1 / 111_320;
    let raf;
    let last = 0;
    const tick = (ts) => {
      raf = requestAnimationFrame(tick);
      if (ts - last < 500) return; // throttle to 2 Hz React updates
      last = ts;
      const now = Date.now() / 1000;
      const result = Object.values(fixesRef.current).map((fix: any) => {
        const elapsed = Math.min(now - (fix.ts || now), 60);
        const gs_ms = (fix.gs || 0) * KNOTS_TO_MS;
        if (elapsed > 0 && gs_ms > 0) {
          const track_rad = ((fix.track || 0) * Math.PI) / 180;
          const cos_lat = Math.cos((fix.lat * Math.PI) / 180) || 1e-9;
          return {
            ...fix,
            lat: fix.lat + gs_ms * Math.cos(track_rad) * DEG_PER_M * elapsed,
            lon: fix.lon + (gs_ms * Math.sin(track_rad)) / (111_320 * cos_lat) * elapsed,
          };
        }
        return fix;
      });
      setAnimatedAircraft(result);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  async function handleApply() {
    setSaving(true);
    setSaveMsg(null);
    try {
      const res = await fetch(`${API}/simulation/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        // max_range_km lives in sceneDraft now — a fleet-restart setting,
        // not an in-process spawn fraction. min/max_aircraft default above
        // guarantees these are always numeric, never NaN over the wire.
        body: JSON.stringify({
          frac_anomalous: draft.frac_anomalous,
          frac_drone:     draft.frac_drone,
          frac_dark:      draft.frac_dark,
          min_aircraft:   Number(draft.min_aircraft),
          max_aircraft:   Number(draft.max_aircraft),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      // Adopt the stamp our own PUT produced, so the poll below doesn't read
      // this write back as somebody else's change. A response without one
      // leaves it null, which just re-baselines on the next payload.
      const stamp = body?.config?._updated_at;
      lastStampRef.current = typeof stamp === "number" ? stamp : null;
      dirtyRef.current = false;
      setDrift(null);
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
    dirtyRef.current = true;
    setDraft(prev => ({ ...prev, [key]: frac(pctVal) }));
  }

  // Running scene — zero new plumbing: counts what's actually connected
  // rather than trusting the config the fleet booted with.
  const fetchRunningScene = useCallback(async () => {
    try {
      const res = await fetch(`${API}/radar/nodes`, { signal: abortRef.current?.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (abortRef.current?.signal.aborted) return;
      const entries = Object.entries(data.nodes || {});
      const connectedSynthetic = entries.filter(
        ([, n]: any) => n.is_synthetic && n.status !== "disconnected",
      );
      const dualSites = Math.floor(
        connectedSynthetic.filter(([id]) => /-DUAL-\d{4}[ab]$/.test(id)).length / 2,
      );
      setRunningScene({ connected: connectedSynthetic.length, dualSites });
    } catch {
      // non-fatal — the running-scene line just holds the last good read
    }
  }, []);

  useEffect(() => {
    fetchRunningScene();
    const id = setInterval(fetchRunningScene, 10000);
    return () => clearInterval(id);
  }, [fetchRunningScene]);

  // "restart pending" clears once the connected count has caught up with
  // the draft — generation can come out a couple of nodes short when
  // _pick_illuminator_pair skips a site, so this is a converge check, not
  // an exact-match one.
  useEffect(() => {
    if (!scenePending || !runningScene || !sceneDraft) return;
    if (runningScene.connected >= sceneDraft.n_nodes - 2) {
      setScenePending(false);
    }
  }, [scenePending, runningScene, sceneDraft]);

  async function applySceneChange() {
    setSceneApplying(true);
    setSceneMsg(null);
    try {
      const res = await fetch(`${API}/simulation/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        // Only these — never merged with `draft`'s PUT body. max_range_km
        // rides along here (not the main Apply) because applying it, like
        // n_nodes/dual_fraction, requires regenerating node configs —
        // that's the fleet-restart path, not an in-process reapply.
        body: JSON.stringify({
          n_nodes: sceneDraft.n_nodes,
          dual_fraction: sceneDraft.dual_fraction,
          max_range_km: sceneDraft.max_range_km,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const stamp = body?.config?._updated_at;
      lastStampRef.current = typeof stamp === "number" ? stamp : null;
      sceneDirtyRef.current = false;
      setDrift(null);
      setSceneMsg("Applied — fleet is restarting (~30–60s), the map will go quiet briefly.");
      setScenePending(true);
      setTimeout(() => setSceneMsg(null), 6000);
    } catch (e) {
      setSceneMsg(`Error: ${e.message}`);
    } finally {
      setSceneApplying(false);
    }
  }

  function handleSceneApplyClick() {
    if (!sceneArmed) {
      // First click arms; second click (within 5s) confirms and PUTs.
      setSceneArmed(true);
      if (sceneArmTimerRef.current) clearTimeout(sceneArmTimerRef.current);
      sceneArmTimerRef.current = setTimeout(() => setSceneArmed(false), 5000);
      return;
    }
    if (sceneArmTimerRef.current) clearTimeout(sceneArmTimerRef.current);
    setSceneArmed(false);
    applySceneChange();
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

      {/* ── Drift banner ─────────────────────────────────────────────── */}
      {drift && (
        <div className={`ps-drift ps-drift-${drift.kind}`}>
          {drift.kind === "stale" ? (
            <>
              <span>
                The simulator&rsquo;s config changed outside this page
                {drift.reverted ? " (it reverted to boot state — most likely a backend restart)" : ""}
                . Your unapplied edits no longer sit on top of what&rsquo;s running —
                apply them to push, or reload to take the running values.
              </span>
              <button className="ps-drift-btn" onClick={reloadFromSimulator}>
                Reload from simulator
              </button>
            </>
          ) : (
            <>
              <span>
                The simulator&rsquo;s config changed outside this page
                {drift.reverted ? " (it reverted to boot state — most likely a backend restart)" : ""}
                . The sliders now show what the simulator is actually running.
              </span>
              <button className="ps-drift-btn" onClick={() => setDrift(null)}>
                Dismiss
              </button>
            </>
          )}
        </div>
      )}

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
          <div className="ps-settings-label">
            Total objects target
            <span className="ps-settings-sublabel"> (spawns {draft.min_aircraft}–{draft.max_aircraft})</span>
          </div>
          <div className="ps-slider-row">
            <input
              type="range"
              min={5}
              max={200}
              step={5}
              value={draft.max_aircraft}
              onChange={e => {
                const v = Number(e.target.value);
                dirtyRef.current = true;
                setDraft(prev => ({ ...prev, max_aircraft: v, min_aircraft: Math.max(1, Math.floor(v * 0.8)) }));
              }}
              className="ps-range"
              style={{
                "--thumb-color": "#38bdf8",
                "--fill-pct": `${((draft.max_aircraft - 5) / 195) * 100}%`,
              }}
            />
            <span className="ps-pct-val" style={{ color: "#38bdf8", minWidth: "3rem" }}>
              {draft.max_aircraft}
            </span>
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

      {/* ── Fleet Scene ─────────────────────────────────────────────── */}
      {sceneDraft && (() => {
        const dualPct = Math.round(sceneDraft.dual_fraction * 100);
        const dualSitesPreview = Math.round(sceneDraft.n_nodes * sceneDraft.dual_fraction / 2);
        return (
          <div className="ps-scene-section">
            <div className="ps-scene-title">Fleet Scene</div>
            <p className="ps-scene-running">
              {runningScene
                ? `Running: ${runningScene.connected} connected nodes · ${runningScene.dualSites} dual sites`
                : "Running: —"}
            </p>

            <div className="ps-settings-grid">
              <div className="ps-settings-card">
                <div className="ps-settings-label">Node count</div>
                <div className="ps-slider-row">
                  <input
                    type="range"
                    min={10}
                    max={60}
                    step={2}
                    value={sceneDraft.n_nodes}
                    onChange={e => {
                      sceneDirtyRef.current = true;
                      setSceneDraft(prev => ({ ...prev, n_nodes: Number(e.target.value) }));
                    }}
                    className="ps-range"
                    style={{
                      "--thumb-color": "#a78bfa",
                      "--fill-pct": `${((sceneDraft.n_nodes - 10) / 50) * 100}%`,
                    }}
                  />
                  <span className="ps-pct-val" style={{ color: "#a78bfa", minWidth: "3rem" }}>
                    {sceneDraft.n_nodes}
                  </span>
                </div>
              </div>

              <div className="ps-settings-card">
                <div className="ps-settings-label">
                  Dual-node fraction
                  <span className="ps-settings-sublabel">
                    {" "}→ {dualSitesPreview} dual sites ({dualSitesPreview * 2} of {sceneDraft.n_nodes} nodes)
                  </span>
                </div>
                <div className="ps-slider-row">
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={5}
                    value={dualPct}
                    onChange={e => {
                      sceneDirtyRef.current = true;
                      setSceneDraft(prev => ({ ...prev, dual_fraction: Number(e.target.value) / 100 }));
                    }}
                    className="ps-range"
                    style={{
                      "--thumb-color": "#a78bfa",
                      "--fill-pct": `${dualPct}%`,
                    }}
                  />
                  <span className="ps-pct-val" style={{ color: "#a78bfa", minWidth: "3rem" }}>
                    {dualPct}%
                  </span>
                </div>
              </div>

              <div className="ps-settings-card">
                <div className="ps-settings-label">Max detection range</div>
                <div className="ps-slider-row">
                  <input
                    type="range"
                    min={0}
                    max={300}
                    step={10}
                    value={sceneDraft.max_range_km}
                    onChange={e => {
                      sceneDirtyRef.current = true;
                      setSceneDraft(prev => ({ ...prev, max_range_km: Number(e.target.value) }));
                    }}
                    className="ps-range"
                    style={{
                      "--thumb-color": "#a78bfa",
                      "--fill-pct":    `${(sceneDraft.max_range_km / 300) * 100}%`,
                    }}
                  />
                  <span
                    className="ps-pct-val"
                    style={{ color: "#a78bfa", minWidth: "4.5rem" }}
                    title={sceneDraft.max_range_km === 0 ? "Per-node generated ranges (no uniform override)" : undefined}
                  >
                    {sceneDraft.max_range_km === 0 ? "auto" : `${sceneDraft.max_range_km} km`}
                  </span>
                </div>
              </div>
            </div>

            <div className="ps-scene-warning">
              Restarts the fleet simulator: the map goes quiet for ~30–60 s and all synthetic aircraft respawn.
            </div>

            <div className="ps-actions">
              <button
                className={`ps-scene-apply-btn${sceneArmed ? " ps-scene-apply-armed" : ""}`}
                onClick={handleSceneApplyClick}
                disabled={sceneApplying}
              >
                {sceneApplying ? "Applying…" : sceneArmed ? "Confirm restart" : "Apply Scene Change"}
              </button>
              {scenePending && <span className="ps-scene-pending-chip">restart pending…</span>}
              {sceneMsg && (
                <span className={`ps-save-msg ${sceneMsg.startsWith("Error") ? "ps-save-err" : ""}`}>
                  {sceneMsg}
                </span>
              )}
            </div>
          </div>
        );
      })()}

      {/* ── Ground Truth Map ────────────────────────────────────────── */}
      {(animatedAircraft.length > 0 || (gtData && gtData.aircraft.length > 0)) && (() => {
        const ac = animatedAircraft.length > 0 ? animatedAircraft : gtData.aircraft;
        const centerSrc = (gtData && gtData.aircraft.length > 0) ? gtData.aircraft : ac;
        const centerLat = centerSrc.reduce((s, a) => s + a.lat, 0) / centerSrc.length;
        const centerLon = centerSrc.reduce((s, a) => s + a.lon, 0) / centerSrc.length;

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

      {/* ── Solver Report ──────────────────────────────────────────── */}
      {gtData?.performance && (
        <div className="ps-perf-section">
          <div className="ps-perf-title">Solver Report</div>
          <div className="ps-perf-grid">
            <div className="ps-perf-card ps-perf-rate">
              <span className="ps-perf-val">{gtData.performance.detection_rate_pct}%</span>
              <span className="ps-perf-lbl">Detection Rate</span>
            </div>
            <div
              className="ps-perf-card"
              title="Live multinode tracks with no ADS-B tag, more than 5 km from every ground-truth trail and every fresh ADS-B fix"
            >
              <span className="ps-perf-val">
                {solverStats ? solverStats.ghosts.ghost_tracks : "—"}
              </span>
              <span className="ps-perf-lbl">
                Ghost Tracks
                {solverStats && (
                  <span className="ps-perf-sublbl">
                    {" "}· {formatPct(solverStats.ghosts.precision_pct)} precision
                  </span>
                )}
              </span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">
                {solverStats ? formatKm(solverStats.position_error_km.median) : "—"}
              </span>
              <span className="ps-perf-lbl">Median Error</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">
                {solverStats ? formatKm(solverStats.position_error_km.p90) : "—"}
              </span>
              <span className="ps-perf-lbl">p90 Error</span>
            </div>
            <div className="ps-perf-card">
              <span className="ps-perf-val">{gtData.performance.multinode_tracks}</span>
              <span className="ps-perf-lbl">Multinode Tracks</span>
            </div>
          </div>

          {solverStats && (
            <>
              {/* Publication funnel: published n=2 / n≥3 vs rejected, then a
                  proportional mini-bar per reject reason (largest first). */}
              <div className="ps-funnel-section">
                <div className="ps-funnel-bar">
                  {funnelSegments(solverStats)
                    .filter((seg) => seg.count > 0)
                    .map((seg) => (
                      <div
                        key={seg.key}
                        className="ps-funnel-seg"
                        style={{ flex: seg.count, background: seg.color }}
                        title={`${seg.label}: ${seg.count} (${formatPct(seg.pct)})`}
                      />
                    ))}
                </div>
                <div className="ps-funnel-legend">
                  {funnelSegments(solverStats).map((seg) => (
                    <span key={seg.key} style={{ color: seg.color }}>
                      ■ {seg.label}: {seg.count}
                    </span>
                  ))}
                </div>
                <div className="ps-funnel-caption">
                  last {solverStats.window_minutes} min · {solverStats.attempts} attempts
                </div>

                {rejectReasonBars(solverStats.rejects.by_reason).length > 0 && (
                  <div className="ps-reject-reasons">
                    {rejectReasonBars(solverStats.rejects.by_reason).map((r) => (
                      <div key={r.reason} className="ps-reject-row">
                        <span className="ps-reject-label">{r.reason}</span>
                        <div className="ps-reject-track">
                          <div className="ps-reject-fill" style={{ width: `${r.pct}%` }} />
                        </div>
                        <span className="ps-reject-count">{r.count}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Consensus: mode badge + since-boot counters. */}
              <div className="ps-consensus-row">
                <span className={`ps-consensus-badge ps-consensus-${solverStats.consensus.mode}`}>
                  {consensusModeLabel(solverStats.consensus.mode)}
                </span>
                <span className="ps-consensus-chip">Selected {solverStats.consensus.selected}</span>
                <span className="ps-consensus-chip">Filtered {solverStats.consensus.filtered}</span>
                <span className="ps-consensus-chip">Fallback {solverStats.consensus.fallback}</span>
                <span className="ps-consensus-chip">Shadow {solverStats.consensus.shadow}</span>
                <span className="ps-consensus-caption">since boot</span>
              </div>

              {/* Claiming: mode badge + since-boot counters. Absent from
                  older cached payloads (claiming/fragmentation shipped
                  later than consensus), so both render null-safely. */}
              {solverStats.claiming && (
                <div className="ps-consensus-row">
                  <span className={`ps-consensus-badge ps-consensus-${solverStats.claiming.mode}`}>
                    Claiming: {claimModeLabel(solverStats.claiming.mode)}
                  </span>
                  <span className="ps-consensus-chip">Matched {solverStats.claiming.matched}</span>
                  <span className="ps-consensus-chip">Conflicts {solverStats.claiming.conflicts}</span>
                  <span className="ps-consensus-chip">Anchored {solverStats.claiming.anchored_inputs}</span>
                  <span className="ps-consensus-chip">Fallbacks {solverStats.claiming.anchor_fallbacks}</span>
                  <span className="ps-consensus-caption">since boot</span>
                </div>
              )}
              {solverStats.fragmentation && (
                <div className="ps-consensus-row">
                  <span className="ps-consensus-chip">
                    Distinct keys {solverStats.fragmentation.distinct_keys}
                  </span>
                  <span className="ps-consensus-chip">
                    Solves/key median {solverStats.fragmentation.solves_per_key.median ?? "—"}
                  </span>
                  <span className="ps-consensus-chip">
                    Anchored {formatPct(solverStats.fragmentation.anchored_pct)}
                  </span>
                  <span className="ps-consensus-caption">last {solverStats.window_minutes} min</span>
                </div>
              )}
            </>
          )}
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
