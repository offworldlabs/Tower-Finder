import { useState, useEffect } from "react";
import { fetchMlatAccuracy, fetchMlatVerification, fetchNodeVerification } from "../../api";
import { POSITION_SOURCE_ARC_ONLY } from "./constants";
import { classifyHex, emergencySquawkLabel } from "./hexInfo";
import { trailToCsv, downloadCsv } from "./trailExport";
import { copyToClipboard, toast } from "./toast";
import { M_PER_FT, KNOTS_PER_MS, MS_PER_KNOT } from "./units";

export default function AircraftDetailPanel({ ac, onClose, groundTruth, trails, computeError, detectingNodes = [], solveHistory = null }) {
  if (!ac) return null;

  const err = computeError(ac.hex, ac);
  const gtHex = ac.ground_truth_hex || ac.hex;
  const gtTrail = groundTruth[gtHex];
  const solvedPts = (trails[ac.hex] || []).length;
  const truthPts = gtTrail?.length || 0;
  const gtLast = gtTrail?.length ? gtTrail[gtTrail.length - 1] : null;
  const altErrFt = gtLast ? Math.abs((ac.alt_baro || 0) - gtLast[2] / M_PER_FT) : null;

  // Enthusiast classification — military/govt/test hex ranges and special
  // squawk codes. Both are "always on": even a truth-only entry gets a
  // hex-range badge if it lands in a known range.
  const hexInfo = classifyHex(ac.hex);
  const emergency = emergencySquawkLabel(ac.squawk);

  const handleExportTrail = () => {
    // `trails` (prop) is the canonical solved-position trail buffer maintained
    // by LiveAircraftMap. Backend rows are [lat, lon, alt_ft, ts_ms]; the
    // exporter's normaliseRow handles the legacy 3-tuple form too.
    const rows = ((trails && trails[ac.hex]) || []).filter(Boolean).slice();
    if (!rows.length) {
      // No buffered points — fall back to the current single fix so the user
      // still gets a non-empty CSV.
      if (ac.lat != null && ac.lon != null) {
        rows.push([ac.lat, ac.lon, (ac.alt_baro || 0), Date.now()]);
      } else {
        toast("No trail data yet", { tone: "warn" });
        return;
      }
    }
    const csv = trailToCsv(ac.hex, ac.flight, rows);
    downloadCsv(`tower-finder-trail-${ac.hex}-${Date.now()}.csv`, csv);
    toast(`Exported ${rows.length} points`, { tone: "success" });
  };

  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const isAmbiguityArc = ac.position_source === POSITION_SOURCE_ARC_ONLY;
  const isSolverOnly = ac.position_source === "solver_single_node";
  const isSolverAdsbSeed = ac.position_source === "solver_adsb_seed";
  const isDrone = ac.target_class === "drone";
  const sourceLabel = isMultinode
    ? `Multi-node (${ac.n_nodes}N)`
    : isAmbiguityArc
      ? "Single-node ellipse arc"
      : isSolverAdsbSeed
        ? "Solver (ADS-B seeded)"
        : isSolverOnly
          ? "Single-node solver (uncertain)"
          : hasAdsb
            ? "ADS-B"
            : ac.type || "Unknown";
  const sourceBadge = isMultinode ? "multinode" : isSolverAdsbSeed ? "adsb" : hasAdsb ? "adsb" : "other";
  // Authoritative flag set by applyGroundTruthFixes — the old
  // `!ac.type && !ac.flight` heuristic classified ordinary radar tracks
  // (which usually have neither) as "Ground truth only".
  const isTruthOnly = Boolean(ac._isTruth);

  return (
    <div className="detail-panel">
      <div className="detail-panel-header">
        <h3>{ac.flight?.trim() || ac.hex}</h3>
        <button className="close-btn" onClick={onClose} title="Close">
          &times;
        </button>
      </div>
      <div className="detail-panel-body">
        {emergency && (
          <div
            style={{
              background: "rgba(244, 63, 94, 0.15)",
              border: "1px solid #f43f5e",
              color: "#fecaca",
              padding: "8px 10px",
              borderRadius: 6,
              marginBottom: 10,
              fontWeight: 600,
              fontSize: 13,
            }}
            title="Emergency squawk code reported by aircraft transponder"
          >
            ⚠ {emergency}
          </div>
        )}
        {/* Identity */}
        <div className="detail-section">
          <div className="detail-section-title">Identity</div>
          <Field label="HEX" value={<span className="detail-hex-badge" onClick={() => copyToClipboard(ac.hex, "HEX copied")} title="Click to copy" style={{ cursor: "pointer" }}>{ac.hex}</span>} />
          {hexInfo.label && (
            <Field
              label="Registry"
              value={
                <span
                  style={{
                    color: hexInfo.color,
                    fontWeight: 600,
                    border: `1px solid ${hexInfo.color}`,
                    padding: "1px 6px",
                    borderRadius: 4,
                    fontSize: 11,
                    textTransform: "uppercase",
                    letterSpacing: 0.4,
                  }}
                >
                  {hexInfo.label}
                </span>
              }
            />
          )}
          {!isTruthOnly && (
            <>
              <Field label="Callsign" value={ac.flight?.trim() || "\u2014"} />
              <Field
                label="Source"
                value={<span className={`detail-source-badge ${sourceBadge}`}>{sourceLabel}</span>}
              />
              {ac.target_class && (
                <Field
                  label="Target class"
                  value={
                    <span style={{ color: isDrone ? "#f59e0b" : "#38bdf8", fontWeight: 600 }}>
                      {isDrone ? "\u{1F6F8} Drone" : "\u2708\uFE0F Aircraft"}
                    </span>
                  }
                />
              )}
            </>
          )}
          {isTruthOnly && (
            <Field
              label="Status"
              value={<span className="detail-source-badge other">Ground truth only</span>}
            />
          )}
        </div>

        {/* Position */}
        <div className="detail-section">
          <div className="detail-section-title">Position</div>
          <Field label={isAmbiguityArc ? "Arc midpoint lat" : "Latitude"} value={ac.lat?.toFixed(5) ?? "\u2014"} />
          <Field label={isAmbiguityArc ? "Arc midpoint lon" : "Longitude"} value={ac.lon?.toFixed(5) ?? "\u2014"} />
          <Field
            label="Altitude"
            value={
              ac.alt_baro != null
                ? `${ac.alt_baro.toLocaleString()} ft`
                : ac.alt_m != null
                  ? `${Math.round(ac.alt_m / M_PER_FT).toLocaleString()} ft`
                  : "\u2014"
            }
          />
          {!isTruthOnly && (
            <>
              <Field label="Speed" value={ac.gs != null ? `${ac.gs} kts` : "\u2014"} />
              <Field
                label="Heading"
                value={ac.track != null ? `${ac.track.toFixed(0)}\u00b0` : "\u2014"}
              />
            </>
          )}
          {isTruthOnly && (
            <>
              <Field label="Speed" value={ac.speed_ms != null && ac.speed_ms > 0 ? `${(ac.speed_ms * KNOTS_PER_MS).toFixed(0)} kts (${ac.speed_ms.toFixed(0)} m/s)` : ac.gs != null ? `${ac.gs} kts` : "\u2014"} />
              <Field label="Heading" value={ac.heading != null && ac.heading > 0 ? `${ac.heading.toFixed(0)}\u00b0` : ac.track != null ? `${ac.track.toFixed(0)}\u00b0` : "\u2014"} />
            </>
          )}
          {isAmbiguityArc && (
            <>
              <Field label="Display mode" value="Delay ellipse across detection area" />
              <Field label="Latest delay" value={ac.delay_us != null ? `${ac.delay_us} μs` : "\u2014"} />
            </>
          )}
          {isSolverOnly && (
            <Field
              label="Note"
              value={<span style={{ color: "#94a3b8", fontStyle: "italic" }}>Position uncertain — single node, no arc</span>}
            />
          )}
        </div>

        {/* Multi-node details */}
        {isMultinode && (
          <div className="detail-section">
            <div className="detail-section-title">Multi-node</div>
            <Field label="Nodes" value={ac.n_nodes} />
            <Field label="RMS Delay" value={`${ac.rms_delay ?? "\u2014"} \u03bcs`} />
            <Field label="RMS Doppler" value={`${ac.rms_doppler ?? "\u2014"} Hz`} />
          </div>
        )}

        {/* Anomaly detection */}
        {ac.is_anomalous && (
          <div className="detail-section">
            <div className="detail-section-title" style={{ color: "#f43f5e" }}>
              ⚠ Anomaly Detected
            </div>
            <Field
              label="Type"
              value={
                <span style={{ color: "#f43f5e", fontWeight: 600 }}>
                  {(ac.anomaly_types || []).map(t => ({
                    supersonic: "Supersonic",
                    // Not a claim about the aircraft — a claim about our own
                    // estimate.  Labelled distinctly so a weakly-observable
                    // Doppler geometry is not read as a Mach-1 target.
                    instant_acceleration: "Instant Acceleration",
                    instant_direction_change: "Instant Direction Change",
                    sustained_orbit: "Sustained Orbit",
                    position_mismatch: "GPS Spoof",
                    identity_swap: "Identity Swap",
                    altitude_jump: "Altitude Jump",
                    anomalous_acceleration: "Anomalous Acceleration (>10g)",
                    long_hover: "Long Hover",
                  }[t] || t)).join(", ") || "unknown"}
                </span>
              }
            />
            {ac.max_velocity_ms > 0 && (
              <Field
                label="Max velocity"
                value={`${ac.max_velocity_ms.toFixed(0)} m/s (Mach ${(ac.max_velocity_ms / 343).toFixed(2)})`}
              />
            )}
            {ac.gs != null && (
              <Field
                label="Current speed"
                value={`${ac.gs} kts (${(ac.gs * MS_PER_KNOT).toFixed(0)} m/s)`}
              />
            )}
          </div>
        )}

        {/* Solver residuals for single-node */}
        {!isMultinode && !isTruthOnly && (ac.rms_delay != null || ac.rms_doppler != null) && (
          <div className="detail-section">
            <div className="detail-section-title">Solver Confidence</div>
            <Field label="RMS Delay" value={ac.rms_delay != null ? `${ac.rms_delay} \u03bcs` : "\u2014"} />
            <Field label="RMS Doppler" value={ac.rms_doppler != null ? `${ac.rms_doppler} Hz` : "\u2014"} />
            {ac.delay_us != null && <Field label="Latest Delay" value={`${ac.delay_us} \u03bcs`} />}
            {ac.doppler_hz != null && <Field label="Latest Doppler" value={`${ac.doppler_hz} Hz`} />}
          </div>
        )}

        {/* Solver verification — renders only when this node has truth-matched data */}
        {ac.node_id && (
          <NodeVerificationSection hex={ac.hex} nodeId={ac.node_id} />
        )}

        {/* MLAT solver verification — show when this is a multinode solve */}
        {ac.position_source === "multinode_solve" && (
          <MlatVerificationSection solverHex={ac.hex} />
        )}

        {/* Per-solve history behind this marker (fetched by LiveAircraftMap,
            which also draws the raw solve dots on the map) */}
        {ac.position_source === "multinode_solve" && solveHistory?.hex === ac.hex && (
          <MlatSolveHistorySection history={solveHistory} />
        )}

        {/* Accuracy */}
        <div className="detail-section">
          <div className="detail-section-title">Accuracy</div>
          <Field label="Solved pts" value={solvedPts} />
          <Field label="Truth pts" value={truthPts} />
          {err !== null && (
            <Field
              label="Pos Error"
              value={
                <span className={`detail-value ${err < 2 ? "good" : err < 5 ? "warn" : "bad"}`}>
                  {err.toFixed(2)} km
                </span>
              }
            />
          )}
          {altErrFt !== null && <Field label="Alt Error" value={`${Math.round(altErrFt)} ft`} />}
        </div>

        {/* Simulated parameters + live detection fan-out (debug) */}
        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Simulation (debug)</div>
            <Field
              label="ADS-B"
              value={
                ac.has_adsb
                  ? <span style={{ color: "#34d399", fontWeight: 600 }}>yes</span>
                  : <span style={{ color: "#64748b", fontWeight: 600 }}>no — dark target</span>
              }
            />
            <Field label="Callsign" value={ac.adsb_callsign || "—"} />
            <Field label="Object type" value={ac.object_type || "aircraft"} />
            {ac.is_anomalous && (
              <Field
                label="Anomaly event"
                value={
                  <span style={{ color: "#f43f5e", fontWeight: 600 }}>
                    {ac.anomaly_event || "anomalous"}
                  </span>
                }
              />
            )}
            <Field
              label="Detected by"
              value={
                detectingNodes.length
                  ? (
                    <span style={{ wordBreak: "break-word" }}>
                      {detectingNodes.join(", ")}
                      <span style={{ color: "#64748b" }}> ({detectingNodes.length})</span>
                    </span>
                  )
                  : <span style={{ color: "#64748b" }}>no nodes right now</span>
              }
            />
          </div>
        )}

        {/* Truth-only trail count */}
        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Trail</div>
            <Field label="Points" value={ac.points || 0} />
          </div>
        )}

        {/* Export — handy for enthusiasts pulling tracks into KML/QGIS. */}
        <div className="detail-section" style={{ borderTop: "1px solid #1e293b", paddingTop: 10 }}>
          <button
            type="button"
            onClick={handleExportTrail}
            style={{
              width: "100%",
              background: "#1e293b",
              border: "1px solid #334155",
              color: "#e2e8f0",
              padding: "8px 10px",
              borderRadius: 6,
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
            }}
            title="Download recent positions as CSV (lat, lon, alt, timestamp)"
          >
            ⇩ Export trail (CSV)
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div className="detail-field">
      <span className="detail-label">{label}</span>
      <span className="detail-value">{value}</span>
    </div>
  );
}

function NodeVerificationSection({ hex, nodeId }) {
  // Tagged with the node it came from, so selecting an aircraft on a different
  // node never shows the previous node's numbers while the new fetch is in
  // flight.
  const [fetched, setFetched] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchNodeVerification(nodeId).then((d) => {
        if (!cancelled && d) setFetched({ nodeId, payload: d });
      });
    };
    load();
    const interval = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [nodeId]);

  const data = fetched && fetched.nodeId === nodeId ? fetched.payload : null;
  if (!data || !data.n_matched) return null;

  // Find this aircraft's specific match
  const match = (data.tracks || []).find((t) => t.hex === hex);

  return (
    <div className="detail-section">
      <div className="detail-section-title" style={{ color: "#f97316" }}>
        📡 {String(nodeId).replace(/-retnode$/, "")} Verification
      </div>
      {match && (
        <>
          <Field label="Pos Error" value={
            <span className={match.position_error_km < 5 ? "good" : match.position_error_km < 15 ? "warn" : "bad"}>
              {match.position_error_km.toFixed(1)} km
            </span>
          } />
          <Field label="Vel Error" value={`${match.velocity_error_ms.toFixed(1)} m/s`} />
          <Field label="Alt Error" value={`${match.altitude_error_m} m`} />
        </>
      )}
      <Field label="Tracked" value={`${data.n_matched}/${data.n_tracks}`} />
      {data.position && <Field label="Median Pos" value={`${data.position.median_km} km`} />}
      {data.position && <Field label="P95 Pos" value={`${data.position.p95_km} km`} />}
      {data.velocity && <Field label="Median Vel" value={`${data.velocity.median_ms} m/s`} />}
      {data.altitude && <Field label="Median Alt" value={`${data.altitude.median_m} m`} />}
    </div>
  );
}

function MlatVerificationSection({ solverHex }) {
  const [data, setData] = useState(null);
  const [accuracy, setAccuracy] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      Promise.all([fetchMlatVerification(), fetchMlatAccuracy()]).then(([verification, rolling]) => {
        if (cancelled) return;
        if (verification) setData(verification);
        if (rolling) setAccuracy(rolling);
      });
    };
    load();
    const interval = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  if (!data || !data.n_matched) return null;

  // Match by synthetic solver hex — same hash the backend uses to generate the map hex.
  // More reliable than proximity matching against dead-reckoned frontend positions.
  const match = (data.tracks || []).find((t) => t.solver_hex === solverHex);
  const nodeStats = match && accuracy?.by_node_count
    ? accuracy.by_node_count[String(match.n_nodes)]
    : null;

  return (
    <div className="detail-section">
      <div className="detail-section-title" style={{ color: "#e879f9" }}>
        MLAT Verification
      </div>
      {match && (
        <>
          <Field label="Pos Error" value={
            <span className={match.position_error_km < 3 ? "good" : match.position_error_km < 8 ? "warn" : "bad"}>
              {match.position_error_km.toFixed(1)} km
            </span>
          } />
          <Field label="Vel Error" value={`${match.velocity_error_ms.toFixed(1)} m/s`} />
          <Field label="Alt Error" value={`${match.altitude_error_m} m`} />
          <Field label="Nodes" value={match.n_nodes} />
        </>
      )}
      {/* The pct is n_matched / n_unique_aircraft (duplicate solver cycles for
          one aircraft collapse), so render the same denominator — showing
          n_solves here made "2/5 (50%)" look like a math error. Falls back to
          n_solves for payloads predating n_unique_aircraft. */}
      <Field label="Match rate" value={`${data.n_matched}/${data.n_unique_aircraft ?? data.n_solves} (${data.match_rate_pct}%)`} />
      {data.position && <Field label="Median Pos" value={`${data.position.median_km} km`} />}
      {data.position && <Field label="P95 Pos" value={`${data.position.p95_km} km`} />}
      {accuracy?.n_samples > 0 && <Field label="Rolling N" value={accuracy.n_samples} />}
      {accuracy?.n_samples > 0 && <Field label="Rolling P95" value={`${accuracy.p95_km} km`} />}
      {nodeStats && <Field label={`Nodes=${match.n_nodes} P95`} value={`${nodeStats.p95_km} km`} />}
    </div>
  );
}

function MlatSolveHistorySection({ history }) {
  // Per-solve records behind this marker over the last ~30 min, newest first
  // (GET /api/test/mlat-history?hex=...).  gt_error_km is frozen at solve
  // time against the nearest GT trail point — independent of the display's
  // dead-reckoning and of the feed's per-frame ground_truth_hex re-binding —
  // so a hex change down the gt column is a visible GT re-bind.
  const solves = history?.solves || [];
  const rejects = history?.rejects_nearby;
  if (!solves.length && !rejects?.n) return null;

  const errClass = (e) => (e == null ? "" : e < 3 ? "good" : e < 8 ? "warn" : "bad");
  // Direction error color, same threshold-bucket idiom as errClass above but
  // inline — heading_err_deg is None whenever truth is near-hover or the
  // solve has no meaningful velocity, which errClass's km buckets don't fit.
  const hdgErrColor = (e) =>
    e == null ? "#64748b" : e < 15 ? "#34d399" : e < 45 ? "#f59e0b" : "#f43f5e";
  const ago = (ts) => {
    const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    return s < 60 ? `-${s}s` : `-${Math.round(s / 60)}m`;
  };
  const cell = { padding: "2px 6px", whiteSpace: "nowrap" };

  return (
    <div className="detail-section">
      <div className="detail-section-title" style={{ color: "#e879f9" }}>
        Solve History ({solves.length} in {history.window_minutes} min)
      </div>
      {solves.length > 0 && (
        <div style={{ maxHeight: 180, overflowY: "auto", fontSize: 11 }}>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr style={{ color: "#64748b", textAlign: "left" }}>
                <th style={cell}>t</th>
                <th style={cell}>N</th>
                <th style={cell}>GT err</th>
                <th style={cell}>Δhdg</th>
                <th style={cell}>rmsD</th>
                <th style={cell}>rmsF</th>
                <th style={cell}>truth</th>
              </tr>
            </thead>
            <tbody>
              {solves.map((s, i) => (
                <tr key={`${s.ts_ms}-${i}`} style={{ borderTop: "1px solid #1e293b" }}>
                  <td style={{ ...cell, color: "#94a3b8" }}>{ago(s.ts_ms)}</td>
                  <td style={cell}>{s.n_nodes}</td>
                  <td style={cell}>
                    <span className={errClass(s.gt_error_km)}>
                      {s.gt_error_km != null ? `${s.gt_error_km.toFixed(2)} km` : "—"}
                    </span>
                  </td>
                  <td style={cell}>
                    <span style={{ color: hdgErrColor(s.heading_err_deg) }}>
                      {s.heading_err_deg != null ? `${s.heading_err_deg}°` : "—"}
                    </span>
                  </td>
                  <td style={cell}>{s.rms_delay}</td>
                  <td style={cell}>{s.rms_doppler}</td>
                  <td style={{ ...cell, color: "#94a3b8" }}>{s.gt_hex || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rejects?.n > 0 && (
        <Field
          label="Rejects nearby"
          value={
            <span style={{ color: "#f59e0b" }} title="Gate-rejected solves within 10 km of the latest published solve">
              {Object.entries(rejects.by_outcome || {})
                .map(([k, v]) => `${k.replace(/^rejected_|^n2_/, "")}:${v}`)
                .join("  ") || rejects.n}
            </span>
          }
        />
      )}
    </div>
  );
}
