// @ts-nocheck — gradual TS migration
/* ── One forget path for the per-object animation stores.
 *
 * LiveAircraftMap keeps eight stores keyed by one string (fixes, smooth,
 * DOM-element cache + its negative cache, Leaflet LatLng cache, two trail
 * buffers, and the marker registry).  Before this module there were THREE
 * hand-maintained prune paths that each knew about a different subset (5, 2
 * and 3 stores respectively) — the exact bug class behind the blue-dot
 * jumping and the never-pruned LatLng cache.  Adding a ninth store now means
 * adding one line to forgetTrack, not remembering three unrelated places. ── */

/** Remove every per-object record for one store key. */
export function forgetTrack(key: string, s) {
  delete s.fixes[key];
  delete s.smooth[key];
  delete s.svgElems[key];
  delete s.svgMiss[key];
  delete s.latLng[key];
  delete s.trails[key];
  delete s.lastTrailSample[key];
  s.markerRegistry?.delete(key);
}

/**
 * Teleport handling: snap the smoothed position to a new fix and drop the
 * motion history, WITHOUT forgetting the object.  Preserves the original
 * guard's semantics exactly — smooth and the cached LatLng are mutated in
 * place (the render loop holds references to them), trails are dropped so
 * the polyline doesn't draw through the discontinuity.
 */
export function snapTrack(key: string, s, lat: number, lon: number, track: number) {
  const sm = s.smooth[key];
  if (sm) {
    sm.lat = lat;
    sm.lon = lon;
  } else {
    s.smooth[key] = { lat, lon, track: track || 0 };
  }
  const cachedLL = s.latLng[key];
  if (cachedLL) {
    cachedLL.lat = lat;
    cachedLL.lng = lon;
  }
  delete s.trails[key];
  delete s.lastTrailSample[key];
}

/** Radar entries not seen within staleMs are forgotten (truth-only skipped —
 *  the ground-truth prune/sweep owns those). */
export function sweepStaleRadar(s, now: number, staleMs: number) {
  for (const key of Object.keys(s.fixes)) {
    const f = s.fixes[key];
    if (f._isTruth) continue;
    if (now - (f._updatedAt ?? 0) > staleMs) forgetTrack(key, s);
  }
}
