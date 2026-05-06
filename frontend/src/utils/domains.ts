/**
 * Single source of truth for hostname-based feature flags.
 *
 * The backend serves several user-facing surfaces from one app, distinguished
 * only by subdomain. Each predicate here captures one concrete decision:
 *
 *   isMapDomain         — any "map" surface (production or staging, real or
 *                         testmap). Used to default to the Live Radar tab and
 *                         hide tower search.
 *   usesRealOnlyFeed    — the production live-radar surface (`map.retina.fm`).
 *                         Hits /ws/aircraft/live so the synthetic fleet never
 *                         appears, even if a node leaks through a bad filter.
 *   defaultsGroundTruthOff — production map AND staging-map. These are the
 *                         "real radar only" surfaces where ADS-B ground truth
 *                         should be off by default; testmap variants leave
 *                         it on for the simulation demo.
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

export const isMapDomain = /^((staging-)?(test)?map)\./i.test(HOSTNAME);
export const usesRealOnlyFeed = /^map\./i.test(HOSTNAME);
export const defaultsGroundTruthOff = /^(staging-)?map\./i.test(HOSTNAME);
