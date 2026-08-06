// Bistatic-ellipse ambiguity arc builder — JS port of
// backend/services/track_gates.py::_build_single_node_arc.
//
// Used to rebuild the displayed arc client-side from the MEASURED delay_us
// (with an optional target-altitude correction the backend's 2D arc lacks).
// The arc is the full delay ellipse clipped to the node's detection area
// (differential-range limit or monostatic circle, plus the beam wedge when
// the node genuinely has one) — it is NOT trimmed around the aircraft
// position, so it represents every position consistent with the measured
// delay.

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
   * RX/TX altitudes (m ASL), used by the 3D altitude correction.  The
   * analytics detection_area payload currently emits rx/tx as {lat, lon}
   * only, so these are typically absent — the builder then falls back to
   * h_rx = h_tx = 0.
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
 * When `altitudeM` (target altitude, meters ASL) is given, the per-bearing
 * search matches the measured delay against the 3D differential — the
 * measured delay is a slant-path quantity, so a 2D match overstates the
 * ground range by up to the altitude itself near the RX.  For a candidate
 * ground point at RX ground-range g, TX ground-range g_tx, target altitude
 * h, RX altitude h_rx, TX altitude h_tx (all km):
 *
 *   differential_3d = sqrt(g² + (h−h_rx)²) + sqrt(g_tx² + (h−h_tx)²)
 *                     − sqrt(ground_baseline² + (h_tx−h_rx)²)
 *
 * Node altitudes come from NodeGeometry.rx_alt_m / tx_alt_m; when the feed
 * doesn't carry them (the analytics detection_area payload currently emits
 * rx/tx as {lat, lon} only) they default to 0.  No altitudeM → the previous
 * pure-2D behavior.  Mirrors the identical formula in
 * backend/services/track_gates.py.
 *
 * Returns null when geometry is missing or the arc can't be constructed
 * (e.g. delay too large for any in-area range, or — with altitude — the
 * delay smaller than the altitude-implied minimum, meaning no ground point
 * at that altitude satisfies the measurement).
 */
export function buildBistaticArc(
  delayUs: number,
  node: NodeGeometry,
  altitudeM?: number | null,
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

  // 3D altitude correction (km).  altKm === null → pure 2D differential
  // (legacy behavior).  Node altitudes default to 0 when the feed doesn't
  // provide them (see NodeGeometry.rx_alt_m / tx_alt_m).
  const altKm =
    altitudeM != null && Number.isFinite(altitudeM) ? altitudeM / 1000 : null;
  const rxAltKm = Number(node.rx_alt_m ?? 0) / 1000;
  const txAltKm = Number(node.tx_alt_m ?? 0) / 1000;
  const baseline3dKm =
    altKm != null ? Math.hypot(baselineKm, txAltKm - rxAltKm) : baselineKm;

  const differentialAt = (rangeKm: number, bearingDegArg: number): number => {
    const bearingRad = (bearingDegArg * Math.PI) / 180;
    const eastKm = Math.sin(bearingRad) * rangeKm;
    const northKm = Math.cos(bearingRad) * rangeKm;
    const txDistKm = Math.hypot(eastKm - txEastKm, northKm - txNorthKm);
    if (altKm == null) return txDistKm + rangeKm - baselineKm;
    return (
      Math.hypot(rangeKm, altKm - rxAltKm) +
      Math.hypot(txDistKm, altKm - txAltKm) -
      baseline3dKm
    );
  };

  // With altitude, the differential at ground-range 0 (directly above the
  // RX) is the bearing-independent minimum of the 3D differential's radial
  // profile along any bearing's near end.  If even that exceeds the measured
  // differential, no ground point at this altitude satisfies the delay —
  // the measurement is inconsistent with the reported altitude.  (In 2D
  // differentialAt(0, ·) is exactly 0, so this guard never fires.)
  if (differentialAt(0, 0) > differentialRangeKm) return null;

  // Per-bearing binary-search ceiling.  With a declared differential limit
  // the whole ellipse passes or fails at once (every point shares the
  // measured differential); the search ceiling is then D/2 + baseline, a
  // bound the locus provably never exceeds (with altitude, the 3D focal
  // separation baseline3d is the applicable — slightly larger — bound).
  // Without one, the monostatic circle clips per bearing.
  let searchMaxKm: number;
  const maxBistaticKm = node.max_bistatic_range_km;
  if (maxBistaticKm != null && Number.isFinite(maxBistaticKm)) {
    if (differentialRangeKm > Number(maxBistaticKm)) return null;
    searchMaxKm = differentialRangeKm / 2 + baseline3dKm;
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
