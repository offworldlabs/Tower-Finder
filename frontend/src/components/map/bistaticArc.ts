// Bistatic-ellipse ambiguity arc builder — JS port of
// backend/services/track_gates.py::_build_single_node_arc.
//
// Used to rebuild the displayed arc client-side from the MEASURED delay_us.
// The arc is the full delay ellipse clipped to the node's detection area
// (differential-range limit or monostatic circle, plus the beam wedge when
// the node genuinely has one) — it is NOT trimmed around the aircraft
// position, so it represents every position consistent with the measured
// delay.
//
// Ground-projected, altitude-agnostic by design (2026-08 direction): the
// arc is a pure function of the measured delay and the node's detection-area
// geometry — target altitude never enters it.  A high-altitude target's arc
// therefore sits a few km outside its ground position; that is the intended
// raw-measurement picture, not a bug.

export interface NodeGeometry {
  rx_lat: number;
  rx_lon: number;
  /**
   * Optional un-fuzzed RX coordinates. The display layer applies a ~400 m
   * privacy fuzz to rx_lat/rx_lon for the visible marker; when present, the
   * arc rebuilder uses these true coords so the curve aligns with the
   * backend-computed locus (which uses the real RX position).
   */
  rx_lat_real?: number | null;
  rx_lon_real?: number | null;
  tx_lat: number;
  tx_lon: number;
  /**
   * RX/TX altitudes (m ASL).  No longer read by this builder (arcs are
   * altitude-agnostic, 2026-08) — kept on the type because other callers
   * populate and pass through NodeGeometry objects carrying them (see
   * hooks.ts / types.ts).
   */
  rx_alt_m?: number | null;
  tx_alt_m?: number | null;
  beam_azimuth_deg?: number | null;
  beam_width_deg?: number | null;
  max_range_km?: number | null;
  /** Differential-range detection limit (km), when the node declares one. */
  max_bistatic_range_km?: number | null;
}

export function bearingDeg(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const lat1r = (lat1 * Math.PI) / 180;
  const lat2r = (lat2 * Math.PI) / 180;
  const dlonr = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dlonr) * Math.cos(lat2r);
  const x =
    Math.cos(lat1r) * Math.sin(lat2r) -
    Math.sin(lat1r) * Math.cos(lat2r) * Math.cos(dlonr);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

function enuToLla(
  rxLat: number, rxLon: number, eastKm: number, northKm: number,
): [number, number] {
  const cosLat = Math.max(0.1, Math.cos((rxLat * Math.PI) / 180));
  const lat = rxLat + northKm / 111.32;
  const lon = rxLon + eastKm / (111.32 * cosLat);
  return [lat, lon];
}

/**
 * Build the bistatic-ambiguity arc for one detection.
 *
 * The arc is the locus of points where the bistatic delay equals delayUs,
 * clipped to the node's detection area: the differential-range limit when
 * the node declares max_bistatic_range_km (else the monostatic max_range_km
 * circle), and the beam wedge when the node genuinely has one (a declared
 * width >= 360° means omnidirectional → the full closed ellipse).
 *
 * Mirrors backend/services/track_gates.py::_build_single_node_arc — the arc
 * is deliberately NOT trimmed around the aircraft's own position: it shows
 * every position the aircraft could occupy given the measured delay.
 *
 * Ground-projected, altitude-agnostic by design (2026-08 direction): this
 * builder is a pure function of delayUs and the node's detection-area
 * geometry — target altitude is never consulted, so a high-altitude
 * target's arc sits a few km outside its ground position.  That is the
 * intended raw-measurement picture, not a bug; see the backend docstring
 * for the reasoning.
 *
 * Returns null when geometry is missing or the arc can't be constructed
 * (e.g. delay too large for any in-area range).
 */
export function buildBistaticArc(
  delayUs: number,
  node: NodeGeometry,
): [number, number][] | null {
  if (!delayUs || delayUs <= 0) return null;
  const rx_lat = node.rx_lat_real ?? node.rx_lat;
  const rx_lon = node.rx_lon_real ?? node.rx_lon;
  const { tx_lat, tx_lon } = node;
  if (rx_lat == null || rx_lon == null || tx_lat == null || tx_lon == null) {
    return null;
  }

  const beamWidthDeg = Number(node.beam_width_deg ?? 42);
  const maxRangeKm = Number(node.max_range_km ?? 50);
  let beamAzimuthDeg = node.beam_azimuth_deg;
  if (beamAzimuthDeg == null) {
    beamAzimuthDeg = (bearingDeg(rx_lat, rx_lon, tx_lat, tx_lon) + 90) % 360;
  }

  const cosLat = Math.max(0.1, Math.cos(((rx_lat + tx_lat) / 2 * Math.PI) / 180));
  const txEastKm = (tx_lon - rx_lon) * 111.32 * cosLat;
  const txNorthKm = (tx_lat - rx_lat) * 111.32;
  const baselineKm = Math.hypot(txEastKm, txNorthKm);
  const differentialRangeKm = delayUs * 0.299792458;

  const differentialAt = (rangeKm: number, bearingDegArg: number): number => {
    const bearingRad = (bearingDegArg * Math.PI) / 180;
    const eastKm = Math.sin(bearingRad) * rangeKm;
    const northKm = Math.cos(bearingRad) * rangeKm;
    const txDistKm = Math.hypot(eastKm - txEastKm, northKm - txNorthKm);
    return txDistKm + rangeKm - baselineKm;
  };

  // differentialAt(0, ·) is exactly 0 for every bearing in the 2-D locus
  // (RX's own ground point is always inside it), and differentialRangeKm is
  // strictly positive here — so this can never actually reject.  Kept as
  // the explicit invariant check rather than assumed silently.
  if (differentialAt(0, 0) > differentialRangeKm) return null;

  // Per-bearing binary-search ceiling.  With a declared differential limit
  // the whole ellipse passes or fails at once (every point shares the
  // measured differential); the search ceiling is then D/2 + baseline, a
  // bound the locus provably never exceeds.  Without one, the monostatic
  // circle clips per bearing.
  let searchMaxKm: number;
  const maxBistaticKm = node.max_bistatic_range_km;
  if (maxBistaticKm != null && Number.isFinite(maxBistaticKm)) {
    if (differentialRangeKm > Number(maxBistaticKm)) return null;
    searchMaxKm = differentialRangeKm / 2 + baselineKm;
  } else {
    searchMaxKm = maxRangeKm;
  }

  // Sweep exactly the in-area bearing interval, centred on the boresight so
  // the midpoint sits on the boresight crossing (matches the backend, whose
  // arc midpoint becomes the track's displayed position).
  let sweepWidthDeg: number;
  let steps: number;
  if (beamWidthDeg >= 360) {
    sweepWidthDeg = 360;
    steps = 72;
  } else {
    sweepWidthDeg = beamWidthDeg;
    steps = 36;
  }
  const centreBearing = beamAzimuthDeg;

  const halfSweep = sweepWidthDeg / 2;
  const points: [number, number][] = [];
  for (let step = 0; step <= steps; step++) {
    const bearing = centreBearing - halfSweep + sweepWidthDeg * (step / steps);
    let lo = 0;
    let hi = searchMaxKm;
    if (differentialAt(hi, bearing) < differentialRangeKm) continue;
    for (let i = 0; i < 32; i++) {
      const mid = (lo + hi) / 2;
      if (differentialAt(mid, bearing) < differentialRangeKm) lo = mid;
      else hi = mid;
    }
    const bearingRad = (bearing * Math.PI) / 180;
    points.push(enuToLla(
      rx_lat, rx_lon, hi * Math.sin(bearingRad), hi * Math.cos(bearingRad),
    ));
  }

  if (points.length < 2) return null;
  return points;
}
