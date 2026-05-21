export default function Toolbar({
  connected,
  paused,
  aircraftCount,
  anomalyCount,
  showCoverage,
  showLabels,
  showTrails,
  showGroundTruth,
  showAnomaliesOnly,
  showIlluminators,
  onToggleCoverage,
  onToggleLabels,
  onToggleTrails,
  onToggleGroundTruth,
  onToggleAnomaliesOnly,
  onToggleIlluminators,
  onTogglePause,
  onFit,
}) {
  return (
    <div className="live-map-toolbar">
      <span className={`connection-badge ${connected ? "connected" : "disconnected"}`}>
        {connected ? (paused ? "PAUSED" : "LIVE") : "POLL"}
      </span>
      <span className="aircraft-count">{aircraftCount} aircraft</span>

      <div className="toolbar-separator" />

      <button className={`toggle-btn${showCoverage ? " active" : ""}`} onClick={onToggleCoverage}>
        Coverage
      </button>
      <button className={`toggle-btn${showIlluminators ? " active" : ""}`} onClick={onToggleIlluminators}>
        Illuminators
      </button>
      <button className={`toggle-btn${showLabels ? " active" : ""}`} onClick={onToggleLabels}>
        Labels
      </button>
      <button className={`toggle-btn${showTrails ? " active" : ""}`} onClick={onToggleTrails}>
        Trails
      </button>
      <button
        className={`toggle-btn${showGroundTruth ? " active" : ""}`}
        onClick={onToggleGroundTruth}
      >
        Debug Truth
      </button>
      <button
        className={`toggle-btn${showAnomaliesOnly ? " active" : ""}`}
        onClick={onToggleAnomaliesOnly}
        style={showAnomaliesOnly ? { background: "#f43f5e", color: "#fff" } : {}}
      >
        ⚠ Anomalies{anomalyCount > 0 ? ` (${anomalyCount})` : ""}
      </button>

      <div className="toolbar-separator" />

      <button className={`toggle-btn${paused ? " active" : ""}`} onClick={onTogglePause}>
        {paused ? "▶ Resume" : "⏸ Pause"}
      </button>
      <button className="toggle-btn" onClick={onFit}>
        ◎ Fit
      </button>

      <span className="map-legend">
        <span className="legend-item">
          <span className="legend-dot" style={{ background: "#2dd4bf" }} /> Solver+ADS-B
        </span>
        <span className="legend-item">
          <span className="legend-dot" style={{ background: "#a78bfa" }} /> Multi
        </span>
        {showGroundTruth && (
          <span className="legend-item">
            <span className="legend-dot" style={{ background: "#2dd4bf" }} /> Truth
          </span>
        )}
        <span className="legend-item">
          <span className="legend-dot" style={{ background: "#facc15" }} /> Node
        </span>
        {showIlluminators && (
          <span className="legend-item">
            <span className="legend-dot" style={{ background: "#f472b6" }} /> Illuminator
          </span>
        )}
      </span>
    </div>
  );
}
