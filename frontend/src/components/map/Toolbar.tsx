import { ALTITUDE_LEGEND } from "./icons";

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
  colorByAlt,
  followSelected,
  showFilters,
  showStats,
  showRangeRings,
  showInBeamDiag,
  soundOn,
  tileTheme,
  hasUserLoc,
  onToggleCoverage,
  onToggleLabels,
  onToggleTrails,
  onToggleGroundTruth,
  onToggleAnomaliesOnly,
  onToggleIlluminators,
  onToggleColorByAlt,
  onToggleFollow,
  onToggleFilters,
  onToggleStats,
  onToggleRangeRings,
  onToggleInBeamDiag,
  onToggleSound,
  onCycleTheme,
  onShare,
  onLocate,
  onExportAll,
  onShowHelp,
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
      <button className={`toggle-btn${colorByAlt ? " active" : ""}`} onClick={onToggleColorByAlt}>
        Alt color
      </button>
      <button className={`toggle-btn${showFilters ? " active" : ""}`} onClick={onToggleFilters}>
        Filters
      </button>
      <button className={`toggle-btn${followSelected ? " active" : ""}`} onClick={onToggleFollow}>
        Follow
      </button>
      <button className={`toggle-btn${showRangeRings ? " active" : ""}`} onClick={onToggleRangeRings} title="Show 5/10/20 km range rings around the selected aircraft">
        Range
      </button>
      <button className={`toggle-btn${showInBeamDiag ? " active" : ""}`} onClick={onToggleInBeamDiag} title="Show red lines from a node to in-beam aircraft it is NOT currently detecting (beam-coverage gaps)">
        Beam gaps
      </button>
      <button className={`toggle-btn${showStats ? " active" : ""}`} onClick={onToggleStats} title="Show / hide the live stats panel (s)">
        Stats
      </button>

      <div className="toolbar-separator" />

      <button className={`toggle-btn${paused ? " active" : ""}`} onClick={onTogglePause}>
        {paused ? "▶ Resume" : "⏸ Pause"}
      </button>
      <button className="toggle-btn" onClick={onFit}>
        ◎ Fit
      </button>
      <button className={`toggle-btn${hasUserLoc ? " active" : ""}`} onClick={onLocate} title="Center on my GPS location (m)">
        📍 Me
      </button>
      <button className="toggle-btn" onClick={onShare} title="Copy a shareable link to this view">
        🔗 Share
      </button>
      <button className="toggle-btn" onClick={onExportAll} title="Export all visible trails as CSV (Shift+X)">
        ⇩ CSV
      </button>
      <button className={`toggle-btn${soundOn ? " active" : ""}`} onClick={onToggleSound} title="Audio alert on emergency squawks (n)">
        {soundOn ? "🔔" : "🔕"}
      </button>
      <button className="toggle-btn" onClick={onCycleTheme} title={`Map theme: ${tileTheme}`}>
        🗺 {tileTheme === "voyager" ? "Dark" : tileTheme === "positron" ? "Light" : "OSM"}
      </button>
      <button className="toggle-btn" onClick={onShowHelp} title="Keyboard shortcuts (?)">
        ?
      </button>

      <span className="map-legend">
        {colorByAlt ? (
          ALTITUDE_LEGEND.map(([c, lbl]) => (
            <span className="legend-item" key={lbl}>
              <span className="legend-dot" style={{ background: c }} /> {lbl}
            </span>
          ))
        ) : (
        <>
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
        </>
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
