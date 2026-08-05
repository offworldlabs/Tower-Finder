/**
 * Single source of truth for hostname-based feature flags.
 *
 * The backend serves several user-facing surfaces from one app, distinguished
 * only by subdomain. Each predicate here captures one concrete decision:
 *
 *   isMapDomain         — any "map" surface, on any environment. Used to default
 *                         to the Live Radar tab and hide tower search.
 *   usesRealOnlyFeed    — hits /ws/aircraft/live so the synthetic fleet never
 *                         appears, even if a node leaks through a bad filter.
 *   defaultsGroundTruthOff — ADS-B ground truth starts hidden.
 *
 * The last two are both "is this the production real-radar surface?", which is
 * `map.retina.fm` and nothing else: every other map surface (prod testmap,
 * staging, the test droplet, the laptop) is fed by the synthetic fleet, where
 * the real-only feed would be empty and the ground-truth overlay is the point.
 * They stay separate exports because the call sites read better naming the
 * decision rather than the environment, but they are deliberately one test.
 *
 * Hostnames covered by isMapDomain:
 *   map.*  testmap.*                  production
 *   staging-map.*                     staging (no staging-testmap; see
 *                                     deploy/nginx-staging.conf)
 *   test-map.*                        the retina-test droplet
 *   testmap.localhost                 the laptop Docker stack
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// The production real-radar surface. Anchored to `map.` exactly: `staging-map`
// and `test-map` are synthetic and must NOT match.
const isProdRealRadar = /^map\./i.test(HOSTNAME);

export const isMapDomain = /^((staging-|test-)?(test)?map)\./i.test(HOSTNAME);
export const usesRealOnlyFeed = isProdRealRadar;
export const defaultsGroundTruthOff = isProdRealRadar;
