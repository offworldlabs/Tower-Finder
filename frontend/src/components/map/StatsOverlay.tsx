import { useMemo } from "react";
import { M_PER_FT } from "./units";

interface StatsOverlayProps {
  aircraft: any[];
  truth: any[];
  anomalyCount: number;
  visible: boolean;
  onToggle: () => void;
}

/**
 * Compact panel pinned to the top-right of the map.  Aggregates the
 * current radar snapshot into the handful of counts an aircraft watcher
 * actually scans for: total tracks, source mix, mean altitude, fastest
 * mover, current anomaly count.
 *
 * The panel is collapsible — collapsed, it shrinks to a single chip so
 * it never hides a target the user is trying to click.
 */
export default function StatsOverlay({ aircraft, truth, anomalyCount, visible, onToggle }: StatsOverlayProps) {
  const stats = useMemo(() => {
    const total = aircraft.length;
    let multinode = 0;
    let arcOnly = 0;
    let adsbSeed = 0;
    let solverOnly = 0;
    let drones = 0;
    let altSum = 0;
    let altCount = 0;
    let maxGs = 0;
    let maxGsCallsign = "";
    for (const ac of aircraft) {
      if (ac.multinode) multinode++;
      else if (ac.position_source === "single_node_ellipse_arc") arcOnly++;
      else if (ac.position_source === "solver_adsb_seed") adsbSeed++;
      else if (ac.position_source === "solver_single_node") solverOnly++;
      if (ac.target_class === "drone") drones++;
      const alt = ac.alt_baro ?? (ac.alt_m ? ac.alt_m / M_PER_FT : null);
      if (alt != null && alt > 0) { altSum += alt; altCount++; }
      const gs = ac.gs ?? 0;
      if (gs > maxGs) { maxGs = gs; maxGsCallsign = (ac.flight || ac.hex || "").trim(); }
    }
    return {
      total,
      truth: truth.length,
      multinode,
      arcOnly,
      adsbSeed,
      solverOnly,
      drones,
      meanAltFt: altCount ? Math.round(altSum / altCount) : null,
      maxGs: Math.round(maxGs),
      maxGsCallsign,
    };
  }, [aircraft, truth]);

  const containerStyle: React.CSSProperties = {
    background: "rgba(2, 6, 23, 0.92)",
    color: "#e2e8f0",
    border: "1px solid #1e293b",
    borderRadius: 8,
    boxShadow: "0 2px 8px rgba(0, 0, 0, 0.4)",
    fontSize: 12,
    minWidth: visible ? 200 : undefined,
    overflow: "hidden",
  };
  const headerStyle: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: visible ? "8px 12px" : "6px 10px",
    cursor: "pointer",
    gap: 8,
    background: visible ? "rgba(15, 23, 42, 0.6)" : "transparent",
    borderBottom: visible ? "1px solid #1e293b" : "none",
    userSelect: "none",
  };
  const bodyStyle: React.CSSProperties = {
    padding: "8px 12px",
    display: "grid",
    gridTemplateColumns: "auto 1fr",
    rowGap: 4,
    columnGap: 10,
    alignItems: "baseline",
  };

  return (
    <div style={containerStyle} role="region" aria-label="Live stats">
      <div style={headerStyle} onClick={onToggle} title={visible ? "Collapse stats" : "Show stats"}>
        <strong style={{ letterSpacing: 0.3 }}>{visible ? "Live stats" : `📊 ${stats.total}`}</strong>
        <span style={{ color: "#94a3b8", fontSize: 11 }}>{visible ? "▲" : "▼"}</span>
      </div>
      {visible && (
        <div style={bodyStyle}>
          <span style={{ color: "#94a3b8" }}>Total</span>
          <span><strong>{stats.total}</strong>{stats.truth ? ` + ${stats.truth} truth` : ""}</span>

          <span style={{ color: "#94a3b8" }}>Multi‑node</span>
          <span><strong style={{ color: "#a78bfa" }}>{stats.multinode}</strong></span>

          <span style={{ color: "#94a3b8" }}>Solver+ADS‑B</span>
          <span><strong style={{ color: "#2dd4bf" }}>{stats.adsbSeed}</strong></span>

          <span style={{ color: "#94a3b8" }}>Arc·1N</span>
          <span>{stats.arcOnly}</span>

          <span style={{ color: "#94a3b8" }}>Solver·1N</span>
          <span>{stats.solverOnly}</span>

          {stats.drones > 0 && (
            <>
              <span style={{ color: "#94a3b8" }}>Drones</span>
              <span style={{ color: "#f59e0b" }}>{stats.drones}</span>
            </>
          )}

          {anomalyCount > 0 && (
            <>
              <span style={{ color: "#94a3b8" }}>Anomalies</span>
              <span style={{ color: "#f43f5e" }}>⚠ {anomalyCount}</span>
            </>
          )}

          {stats.meanAltFt != null && (
            <>
              <span style={{ color: "#94a3b8" }}>Mean alt</span>
              <span>FL{Math.round(stats.meanAltFt / 100)}</span>
            </>
          )}

          {stats.maxGs > 0 && (
            <>
              <span style={{ color: "#94a3b8" }}>Fastest</span>
              <span title={stats.maxGsCallsign}>
                {stats.maxGs} kt
                {stats.maxGsCallsign && (
                  <span style={{ color: "#94a3b8", marginLeft: 4, fontSize: 11 }}>
                    {stats.maxGsCallsign.slice(0, 8)}
                  </span>
                )}
              </span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
