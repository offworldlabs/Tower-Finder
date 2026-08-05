import { useEffect, useRef, useMemo, useState, useCallback } from "react";
import { PLANE_PATH, getAircraftColor } from "./icons";
import { POSITION_SOURCE_ARC_ONLY } from "./constants";
import { classifyHex } from "./hexInfo";
import { distanceKm } from "./distance";
import { M_PER_FT } from "./units";

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
  searchInputRef,
  sortMode = "altitude",
  onSortChange,
  pinned,
  onTogglePin,
  userLoc,
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
    () => {
      const merged = [
        ...allAircraft.map((ac) => ({ ...ac, _isSolved: true })),
        ...truthOnly.map((ac) => ({ ...ac, _isSolved: false })),
      ];
      const pinSet = pinned instanceof Set ? pinned : new Set(pinned || []);
      const altOf = (a) => a.alt_baro ?? (a.alt_m ? a.alt_m / M_PER_FT : 0);
      const distOf = (a) =>
        userLoc && Number.isFinite(a.lat) && Number.isFinite(a.lon)
          ? distanceKm(userLoc.lat, userLoc.lon, a.lat, a.lon)
          : Number.POSITIVE_INFINITY;
      const callsignOf = (a) => (a.flight?.trim() || a.hex || "").toUpperCase();
      const cmp = (a, b) => {
        // Pinned aircraft always sort first regardless of mode.
        const pa = pinSet.has(a.hex) ? 1 : 0;
        const pb = pinSet.has(b.hex) ? 1 : 0;
        if (pa !== pb) return pb - pa;
        switch (sortMode) {
          case "speed":    return (b.gs || 0) - (a.gs || 0);
          case "callsign": return callsignOf(a).localeCompare(callsignOf(b));
          case "distance": return distOf(a) - distOf(b);
          case "altitude":
          default:         return altOf(b) - altOf(a);
        }
      };
      return merged.sort(cmp);
    },
    [allAircraft, truthOnly, pinned, sortMode, userLoc],
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

  const pinSet = pinned instanceof Set ? pinned : new Set(pinned || []);
  const nowMs = Date.now();
  const formatAge = (ac) => {
    // `seen` is seconds since last update (tar1090 convention). Some objects
    // carry `last_seen_ts` (ms) — handle both.
    let sec = null;
    if (Number.isFinite(ac.seen)) sec = ac.seen;
    else if (Number.isFinite(ac.last_seen_ts)) sec = (nowMs - ac.last_seen_ts) / 1000;
    if (sec == null || sec < 0) return null;
    if (sec < 10) return "now";
    if (sec < 60) return `${Math.round(sec)}s`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    return `${Math.round(sec / 3600)}h`;
  };

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
              ref={searchInputRef}
            />
            {searchQuery && (
              <button className="al-clear" onClick={() => onSearchChange("")}>
                ×
              </button>
            )}
          </div>

          <div className="al-sort">
            <label htmlFor="al-sort-select">Sort</label>
            <select
              id="al-sort-select"
              value={sortMode}
              onChange={(e) => onSortChange?.(e.target.value)}
            >
              <option value="altitude">Altitude</option>
              <option value="speed">Speed</option>
              <option value="callsign">Callsign</option>
              <option value="distance" disabled={!userLoc}>
                Distance{userLoc ? "" : " (no GPS)"}
              </option>
            </select>
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
                  // Nullish checks: 0 ft, 0 kt and 0° (due north) are real
                  // values, not absences.
                  const alt = ac.alt_baro != null
                    ? `FL${Math.round(ac.alt_baro / 100)}`
                    : ac.alt_m != null
                      ? `FL${Math.round(ac.alt_m / M_PER_FT / 100)}`
                      : "—";
                  const spd = ac.gs != null ? `${Math.round(ac.gs)}kt` : "—";
                  const hdg = ac.track != null ? `${Math.round(ac.track)}°` : "";
                  const isSelected = ac.hex === selectedHex;
                  const sourceLabel = !isSolved
                    ? "Truth"
                    : ac.multinode
                      ? `Multi·${ac.n_nodes}N`
                      : ac.position_source === POSITION_SOURCE_ARC_ONLY
                        ? "Arc·1N"
                        : ac.position_source === "solver_single_node"
                          ? "Solver·1N"
                          : ac.position_source === "solver_adsb_seed"
                            ? "Solver·ADS-B"
                            : "Solver";
                  const isDrone = ac.target_class === "drone";
                  // Highlight military / govt hex ranges with a small badge
                  // so enthusiasts can scan the list for non-civilian traffic.
                  const hexInfo = classifyHex(ac.hex);

                  return (
                    <div
                      // Solved and truth-only rows can carry the same hex (a
                      // simulated radar track reuses the aircraft's ICAO), so
                      // the store key — namespaced for truth — is the unique one.
                      key={ac._key ?? ac.hex}
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
                        <span className="al-callsign">
                          {onTogglePin && (
                            <button
                              onClick={(e) => { e.stopPropagation(); onTogglePin(ac.hex); }}
                              title={pinSet.has(ac.hex) ? "Unpin" : "Pin to top"}
                              style={{
                                background: "none",
                                border: "none",
                                color: pinSet.has(ac.hex) ? "#facc15" : "#475569",
                                cursor: "pointer",
                                fontSize: 12,
                                padding: 0,
                                marginRight: 4,
                                lineHeight: 1,
                              }}
                            >★</button>
                          )}
                          {callsign}
                          {hexInfo.color && (
                            <span
                              title={hexInfo.label || ""}
                              style={{
                                display: "inline-block",
                                width: 6,
                                height: 6,
                                borderRadius: "50%",
                                background: hexInfo.color,
                                marginLeft: 6,
                                verticalAlign: "middle",
                              }}
                            />
                          )}
                        </span>
                        <span className="al-sub">
                          {sourceLabel}
                          {(() => {
                            const age = formatAge(ac);
                            return age ? ` · ${age}` : "";
                          })()}
                          {userLoc && Number.isFinite(ac.lat) && Number.isFinite(ac.lon)
                            ? ` · ${distanceKm(userLoc.lat, userLoc.lon, ac.lat, ac.lon).toFixed(0)}km`
                            : ""}
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
            </div>
          </div>
        </>
      )}
    </div>
  );
}
