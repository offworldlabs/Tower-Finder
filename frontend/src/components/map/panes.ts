// Custom map panes that make pointer routing deterministic.
//
// Leaflet's canvas renderer containers span the whole viewport and (unlike
// SVG paths) have no pointer-events CSS rule, so with several L.canvas()
// renderers stacked in the overlay pane, ALL map clicks land on whichever
// container happened to be created last — that renderer hit-tests only its
// own layers, and every other canvas (and any SVG stroke below) goes deaf.
// Which container is on top depends on data-arrival order, which is why
// ground-truth dots were only intermittently clickable.
//
// The fix: exactly one canvas keeps pointer events — the ground-truth dot
// canvas, the only one with click handlers — and it sits in its own pane
// just BELOW the overlay pane, so interactive SVG strokes (detection arcs,
// synthetic-node circles) keep priority where they are actually painted.
// Every purely-visual canvas (trails, error lines, verification dots) lives
// in a pointer-events:none pane and can never swallow a click again.
//
// z-order (bottom → top): tiles … debugPassive(398) < gtClick(399) <
// overlayPane(400, SVG) < markerPane(600) < tooltipPane(650).

import type { Map as LeafletMap } from "leaflet";

export const DEBUG_PASSIVE_PANE = "retina-debug-passive";
export const GT_CLICK_PANE = "retina-gt-click";

/** Idempotent; call before adding layers that render into these panes. */
export function ensureDebugPanes(map: LeafletMap): void {
  if (!map.getPane(DEBUG_PASSIVE_PANE)) {
    const p = map.createPane(DEBUG_PASSIVE_PANE);
    p.style.zIndex = "398";
    p.style.pointerEvents = "none";
  }
  if (!map.getPane(GT_CLICK_PANE)) {
    const p = map.createPane(GT_CLICK_PANE);
    p.style.zIndex = "399";
  }
}
