/**
 * Single source of truth for hostname-based feature flags.
 *
 * The backend serves several user-facing surfaces from one app, distinguished
 * only by subdomain. Each predicate here captures one concrete decision:
 *
 *   isMapDomain         — any "map" surface, on any environment. Used to default
 *                         to the Live Radar tab and hide tower search.
 *   usesRealOnlyFeed    — the production live-radar surface (`map.retina.fm`).
 *                         Hits /ws/aircraft/live so the synthetic fleet never
 *                         appears, even if a node leaks through a bad filter.
 *   defaultsGroundTruthOff — production map AND staging-map. These are the
 *                         "real radar only" surfaces where ADS-B ground truth
 *                         should be off by default; testmap variants leave
 *                         it on for the simulation demo.
 *
 * Hostnames covered by isMapDomain:
 *   map.*  testmap.*                  production
 *   staging-map.*  staging-testmap.*  staging
 *   test-map.*  test-testmap.*        the retina-test droplet
 *   testmap.localhost                 the laptop Docker stack
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// The environment prefix is optional and the `test` in `testmap` is separate
// from the `test-` in `test-map.retina.fm`: the first names a synthetic surface
// within an environment, the second names the environment itself.
export const isMapDomain = /^((staging-|test-)?(test)?map)\./i.test(HOSTNAME);
export const usesRealOnlyFeed = /^map\./i.test(HOSTNAME);
export const defaultsGroundTruthOff = /^(staging-)?map\./i.test(HOSTNAME);
