import { useEffect, useRef, useMemo } from "react";
import { PLANE_PATH, getAircraftColor } from "./icons";

export default function AircraftListPanel({
  allAircraft,
  truthOnly,
  selectedHex,
  onSelect,
  collapsed,
  onToggleCollapse,
  searchQuery,
  onSearchChange,
}) {
  const rowRefs = useRef({});

  useEffect(() => {
    if (selectedHex && rowRefs.current[selectedHex]) {
      rowRefs.current[selectedHex].scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [selectedHex]);

  const all = useMemo(
    () =>
      [
        ...allAircraft.map((ac) => ({ ...ac, _isSolved: true })),
        ...truthOnly.map((ac) => ({ ...ac, _isSolved: false })),
      ].sort((a, b) => {
        const altA = a.alt_baro ?? (a.alt_m ? a.alt_m / 0.3048 : 0);
        const altB = b.alt_baro ?? (b.alt_m ? b.alt_m / 0.3048 : 0);
        return altB - altA;
      }),
    [allAircraft, truthOnly],
  );

  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return all;
    const q = searchQuery.toLowerCase();
    return all.filter(
      (ac) =>
        (ac.hex || "").toLowerCase().includes(q) ||
        (ac.flight || "").toLowerCase().includes(q),
    );
  }, [all, searchQuery]);

  return (
    <div className={`aircraft-list-panel${collapsed ? " collapsed" : ""}`}>
      <div className="al-header">
        <div className="al-title">
          {!collapsed && (
            <>
              Aircraft <span className="al-count">{filtered.length}</span>
            </>
          )}
        </div>
        <button
          className="al-collapse-btn"
          onClick={onToggleCollapse}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "▶" : "◀"}
        </button>
      </div>

      {!collapsed && (
        <>
          <div className="al-search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21l-4.35-4.35" />
            </svg>
            <input
              type="text"
              placeholder="Search callsign / hex…"
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
            />
            {searchQuery && (
              <button className="al-clear" onClick={() => onSearchChange("")}>
                ×
              </button>
            )}
          </div>

          <div className="al-list">
            {filtered.length === 0 && <div className="al-empty">No aircraft</div>}
            {filtered.map((ac) => {
              const isSolved = ac._isSolved;
              const color = !isSolved ? "#2dd4bf" : getAircraftColor(ac);
              const callsign =
                ac.flight?.trim() || ac.hex?.slice(-6).toUpperCase() || ac.hex;
              const alt = ac.alt_baro
                ? `FL${Math.round(ac.alt_baro / 100)}`
                : ac.alt_m
                  ? `FL${Math.round(ac.alt_m / 0.3048 / 100)}`
                  : "—";
              const spd = ac.gs ? `${Math.round(ac.gs)}kt` : "—";
              const hdg = ac.track ? `${Math.round(ac.track)}°` : "";
              const isSelected = ac.hex === selectedHex;
              const sourceLabel = !isSolved
                ? "Truth"
                : ac.multinode
                  ? `Multi·${ac.n_nodes}N`
                  : ac.position_source === "single_node_ellipse_arc"
                    ? "Arc·1N"
                    : ac.position_source === "adsb_associated"
                      ? "ADS-B"
                      : "Solver";

              return (
                <div
                  key={ac.hex}
                  ref={(el) => {
                    rowRefs.current[ac.hex] = el;
                  }}
                  className={`al-row${isSelected ? " selected" : ""}${!isSolved ? " truth-only" : ""}`}
                  onClick={() => onSelect(ac.hex)}
                >
                  <div className="al-indicator" style={{ background: color }} />
                  <svg
                    className="al-icon"
                    viewBox="0 0 32 32"
                    fill={color}
                    style={{
                      transform: `rotate(${ac.track ?? 0}deg)`,
                      width: 13,
                      height: 13,
                      flexShrink: 0,
                    }}
                  >
                    <path d={PLANE_PATH} />
                  </svg>
                  <div className="al-info">
                    <span className="al-callsign">{callsign}</span>
                    <span className="al-sub">
                      {sourceLabel}
                    </span>
                  </div>
                  <div className="al-stats">
                    <span className="al-alt">{alt}</span>
                    <span className="al-spd">
                      {spd}
                      {hdg ? ` · ${hdg}` : ""}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
