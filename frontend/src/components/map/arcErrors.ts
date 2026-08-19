// Shortest-distance error from a ground-truth position to an arc-only
// track's ambiguity locus.
//
// An arc-only track's lat/lon is pinned to the arc midpoint (the boresight
// crossing) — a canonical point on the curve, not a position estimate.
// Measuring GT error against that midpoint punishes the arc for a choice of
// convention: the honest error for a curve-shaped claim is the distance from
// truth to the NEAREST point of the curve.  The error line and the panel
// "Pos Error" both use this helper so they always agree.
//
// The locus is rebuilt from the entry's freshest measured delay via
// buildBistaticArc — the same ground-projected curve DetectionArcs draws
// (altitude-agnostic, 2026-08 direction) — never from stale afterglow
// strokes, falling back to the backend-emitted 2D ambiguity_arc when node
// geometry is missing (useNodes still loading).

import { buildBistaticArc } from "./bistaticArc";
import type { NodeGeometry } from "./bistaticArc";
import { nearestPointOnPolyline } from "./geo";

// Rebuilt-locus cache.  Keyed by the same measurement identity the arc
// buffer dedupes on (hex + node + delay); bounded by a wholesale clear — at
// ≤ a few live arc tracks per tick the cache never legitimately grows past
// a few hundred entries.
const _locusCache = new Map<string, [number, number][] | null>();
const _LOCUS_CACHE_MAX = 512;

interface ArcTrackLike {
  hex?: string;
  node_id?: string;
  delay_us?: number;
  alt_baro?: number;
  ambiguity_arc?: [number, number][];
}

/** The drawable locus for an arc-only track entry, or null. */
export function resolveArcPoints(
  ac: ArcTrackLike,
  node: NodeGeometry | undefined | null,
): [number, number][] | null {
  const delay = ac.delay_us;
  if (node && ac.node_id && delay != null && delay > 0) {
    const key = `${ac.hex}|${ac.node_id}|${delay}`;
    let pts = _locusCache.get(key);
    if (pts === undefined) {
      pts = buildBistaticArc(delay, node) ?? null;
      if (_locusCache.size >= _LOCUS_CACHE_MAX) _locusCache.clear();
      _locusCache.set(key, pts);
    }
    if (pts && pts.length >= 2) return pts;
  }
  return Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2
    ? ac.ambiguity_arc
    : null;
}

/**
 * Nearest point on the track's arc to (lat, lon) with the distance in km.
 * Null when no locus is available (caller falls back to point-to-point).
 */
export function arcNearestPoint(
  ac: ArcTrackLike,
  node: NodeGeometry | undefined | null,
  lat: number,
  lon: number,
): { lat: number; lon: number; distKm: number } | null {
  const pts = resolveArcPoints(ac, node);
  if (!pts) return null;
  return nearestPointOnPolyline(lat, lon, pts);
}
