import { useEffect, useRef, useMemo, useState, useCallback } from "react";
import { PLANE_PATH, getAircraftColor } from "./icons";

// Fixed row height must match .al-row CSS (height: 40px, box-sizing: border-box).
// Changing this constant without updating the CSS will break the virtual list.
const ROW_HEIGHT = 40;
const OVERSCAN   = 5; // extra rows to render above/below the visible window

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
  const containerRef     = useRef(null);
  const [scrollTop, setScrollTop]         = useState(0);
  const [containerHeight, setContainerHeight] = useState(600);

  // Keep container height in sync with the panel's flex-allocated size
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setContainerHeight(el.offsetHeight);
    const ro = new ResizeObserver(([entry]) =>
      setContainerHeight(entry.contentRect.height),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const handleScroll = useCallback((e) => {
    setScrollTop(e.currentTarget.scrollTop);
  }, []);

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

  // Scroll the container to bring the selected row into view (programmatic — no DOM refs needed)
  useEffect(() => {
    const el = containerRef.current;
    if (!selectedHex || !el) return;
    const idx = filtered.findIndex((ac) => ac.hex === selectedHex);
    if (idx === -1) return;
    const rowTop = idx * ROW_HEIGHT;
    const { scrollTop: st, offsetHeight } = el;
    if (rowTop < st || rowTop + ROW_HEIGHT > st + offsetHeight) {
      el.scrollTop = rowTop - offsetHeight / 2 + ROW_HEIGHT / 2;
    }
  }, [selectedHex, filtered]);

  // Virtual window — only render rows inside the visible band ± overscan
  const startIdx     = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const endIdx       = Math.min(
    filtered.length,
    Math.ceil((scrollTop + containerHeight) / ROW_HEIGHT) + OVERSCAN,
  );
  const visibleItems = filtered.slice(startIdx, endIdx);
  const totalHeight  = filtered.length * ROW_HEIGHT;
  const offsetY      = startIdx * ROW_HEIGHT;

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

          <div className="al-list" ref={containerRef} onScroll={handleScroll}>
            {filtered.length === 0 && <div className="al-empty">No aircraft</div>}
            {/* Outer div sets the full scroll height; inner div translates to the visible band */}
            <div style={{ height: totalHeight, position: "relative" }}>
              <div style={{ position: "absolute", top: offsetY, left: 0, right: 0 }}>
                {visibleItems.map((ac) => {
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
                        : ac.position_source === "solver_single_node"
                          ? "Solver·1N"
                          : ac.position_source === "adsb_associated"
                            ? "ADS-B"
                            : "Solver";
                  const isDrone = ac.target_class === "drone";

                  return (
                    <div
                      key={ac.hex}
                      className={`al-row${isSelected ? " selected" : ""}${!isSolved ? " truth-only" : ""}`}
                      onClick={() => onSelect(ac.hex)}
                    >
                      <div className="al-indicator" style={{ background: color }} />
                      <svg
                        className="al-icon"
                        viewBox={isDrone ? "0 0 24 24" : "0 0 32 32"}
                        fill={isDrone ? "none" : color}
                        stroke={isDrone ? color : "none"}
                        strokeWidth={isDrone ? 2 : 0}
                        style={{
                          transform: isDrone ? undefined : `rotate(${ac.track ?? 0}deg)`,
                          width: 13,
                          height: 13,
                          flexShrink: 0,
                        }}
                      >
                        {isDrone ? (
                          <>
                            <line x1="4" y1="4" x2="20" y2="20" strokeLinecap="round"/>
                            <line x1="20" y1="4" x2="4" y2="20" strokeLinecap="round"/>
                            <circle cx="12" cy="12" r="2.5" fill={color} stroke="none"/>
                          </>
                        ) : (
                          <path d={PLANE_PATH} />
                        )}
                      </svg>
                      <div className="al-info">
                        <span className="al-callsign">{callsign}</span>
                        <span className="al-sub">{sourceLabel}</span>
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
            </div>
          </div>
        </>
      )}
    </div>
  );
}
