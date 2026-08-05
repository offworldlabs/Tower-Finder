/* One home for unit conversions.  `/ 0.3048` and `* 1.94384` were hand-typed
 * at seven call sites across the panels and the ground-truth ingest. */

export const M_PER_FT = 0.3048;
export const MS_PER_KNOT = 0.514444;
export const KNOTS_PER_MS = 1.94384;

export const mToFt = (m: number) => m / M_PER_FT;
export const msToKnots = (ms: number) => ms * KNOTS_PER_MS;
