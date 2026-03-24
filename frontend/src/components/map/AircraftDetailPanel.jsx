export default function AircraftDetailPanel({ ac, onClose, groundTruth, trails, computeError }) {
  if (!ac) return null;

  const err = computeError(ac.hex, ac);
  const gtHex = ac.ground_truth_hex || ac.hex;
  const gtTrail = groundTruth[gtHex];
  const solvedPts = (trails[ac.hex] || []).length;
  const truthPts = gtTrail?.length || 0;
  const gtLast = gtTrail?.length ? gtTrail[gtTrail.length - 1] : null;
  const altErrFt = gtLast ? Math.abs((ac.alt_baro || 0) - gtLast[2] / 0.3048) : null;

  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const isAmbiguityArc = ac.position_source === "single_node_ellipse_arc";
  const isSolverOnly = ac.position_source === "solver_single_node";
  const isDrone = ac.target_class === "drone";
  const sourceLabel = isMultinode
    ? `Multi-node (${ac.n_nodes}N)`
    : isAmbiguityArc
      ? "Single-node ellipse arc"
      : isSolverOnly
        ? "Single-node solver (uncertain)"
        : ac.position_source === "adsb_associated"
          ? "ADS-B associated"
          : hasAdsb
            ? "ADS-B"
            : ac.type || "Unknown";
  const sourceBadge = isMultinode ? "multinode" : hasAdsb ? "adsb" : "other";
  const isTruthOnly = !ac.type && !ac.flight;

  return (
    <div className="detail-panel">
      <div className="detail-panel-header">
        <h3>{ac.flight?.trim() || ac.hex}</h3>
        <button className="close-btn" onClick={onClose} title="Close">
          &times;
        </button>
      </div>
      <div className="detail-panel-body">
        {/* Identity */}
        <div className="detail-section">
          <div className="detail-section-title">Identity</div>
          <Field label="HEX" value={<span className="detail-hex-badge">{ac.hex}</span>} />
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
                  ? `${Math.round(ac.alt_m / 0.3048).toLocaleString()} ft`
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
          {isAmbiguityArc && (
            <>
              <Field label="Display mode" value="Delay ellipse clipped to beam" />
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

        {/* Truth-only trail count */}
        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Trail</div>
            <Field label="Points" value={ac.points || 0} />
          </div>
        )}
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
