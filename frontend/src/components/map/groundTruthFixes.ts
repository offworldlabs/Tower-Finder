// @ts-nocheck — gradual TS migration
import { groundTruthKey } from "./constants";

/* ── Ground-truth objects in the frontend animation stores.
 *
 * The map keeps four per-object stores — fixesRef (server fix + anchor time),
 * smoothRef (60 fps dead-reckoned position), and two trail buffers — all keyed
 * by one string.  Radar tracks key on their ICAO hex.  In simulation a radar
 * track carries the *same* hex as the aircraft that produced it, so if truth
 * used the bare hex too, the radar ingest and the truth ingest would write one
 * key and the marker would alternate between the solved position and the true
 * one on every 1 Hz update — measured 29.8 km apart for a single-node arc
 * track on staging, which is what "the blue dots jump around" was.
 *
 * Truth therefore lives under a namespaced key while `hex` on the object stays
 * the real hex, so labels, selection and error computation are unaffected.
 * `_key` is the store key and is what the animation loop must index by. ── */

/**
 * Fold a ground-truth snapshot into `fixes` in place.
 *
 * @param fixes   the fixesRef store, mutated
 * @param snapshot  hex → trail, each point [lat, lon, alt_m, ts]
 * @param meta      hex → { speed_ms, heading, object_type, is_anomalous }
 * @param now       ms epoch used as the dead-reckoning anchor
 * @returns the set of store keys this snapshot vouches for
 */
export function applyGroundTruthFixes(fixes, snapshot, meta, now) {
  const activeKeys = new Set();
  for (const [hex, positions] of Object.entries(snapshot || {})) {
    if (!Array.isArray(positions) || positions.length === 0) continue;
    const last = positions[positions.length - 1];
    const m = (meta || {})[hex] || {};
    const lat = last[0], lon = last[1];
    const key = groundTruthKey(hex);
    activeKeys.add(key);
    const prev = fixes[key];
    // Only re-anchor when the fix actually moved; otherwise dead-reckoning
    // would restart from zero elapsed on every re-broadcast and the object
    // would stall between server updates.
    const posChanged = !prev || prev._fixLat !== lat || prev._fixLon !== lon;
    fixes[key] = {
      hex,
      _key: key,
      lat, lon,
      alt_baro: Math.round(last[2] / 0.3048),
      gs: Math.round((m.speed_ms || 0) * 1.94384 * 10) / 10,
      track: m.heading || 0,
      object_type: m.object_type,
      is_anomalous: m.is_anomalous,
      points: positions.length,
      _isTruth: true,
      _fixLat: lat,
      _fixLon: lon,
      _fixTs: posChanged ? now : (prev?._fixTs ?? now),
      _updatedAt: now,
    };
  }
  return activeKeys;
}

/**
 * Drop truth entries the latest snapshot no longer vouches for, from `fixes`
 * and every companion store.  Radar entries are left alone — they have their
 * own staleness rule.
 */
export function pruneGroundTruthFixes(fixes, activeKeys, ...companions) {
  for (const key of Object.keys(fixes)) {
    if (!fixes[key]._isTruth) continue;
    if (activeKeys.has(key)) continue;
    delete fixes[key];
    for (const store of companions) delete store[key];
  }
}
