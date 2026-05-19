// Bistatic-ellipse ambiguity arc builder — JS port of
// backend/services/frame_processor.py::_build_single_node_arc.
//
// Used to recompute the displayed arc segment client-side each frame so it
// follows the dead-reckoned aircraft icon between sparse bistatic detections.
// The locus itself is determined by delay_us (constant once measured); only
// the 25-km trim window slides along the locus as the aircraft moves.

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
  beam_azimuth_deg?: number | null;
  beam_width_deg?: number | null;
  max_range_km?: number | null;
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
 * The arc is the locus of points where the bistatic delay equals delayUs.
 * When targetLat/targetLon are provided we trim to a ~25 km segment centred
 * on the bearing from RX to that target — produces a short visible blip
 * near where the aircraft is now, instead of the full beam-spanning curve.
 *
 * Returns null when geometry is missing or the arc can't be constructed
 * (e.g. aircraft outside beam, or delay too large for any in-beam range).
 */
export function buildBistaticArc(
  delayUs: number,
  node: NodeGeometry,
  targetLat?: number,
  targetLon?: number,
): [number, number][] | null {
  if (!delayUs || delayUs <= 0) return null;
  const rx_lat = node.rx_lat_real ?? node.rx_lat;
  const rx_lon = node.rx_lon_real ?? node.rx_lon;
  const { tx_lat, tx_lon } = node;
  if (rx_lat == null || rx_lon == null || tx_lat == null || tx_lon == null) {
    return null;
  }

  const beamWidthDeg = Number(node.beam_width_deg ?? 41);
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

  let centreBearing: number;
  let sweepWidthDeg: number;
  let steps: number;
  if (targetLat != null && targetLon != null) {
    centreBearing = bearingDeg(rx_lat, rx_lon, targetLat, targetLon);
    const targetEastKm = (targetLon - rx_lon) * 111.32 * cosLat;
    const targetNorthKm = (targetLat - rx_lat) * 111.32;
    const targetRangeKm = Math.max(Math.hypot(targetEastKm, targetNorthKm), 5);
    const BLIP_ARC_LENGTH_KM = 25;
    sweepWidthDeg = Math.min(
      beamWidthDeg,
      (BLIP_ARC_LENGTH_KM / targetRangeKm) * (180 / Math.PI),
    );
    steps = 18;
  } else {
    centreBearing = beamAzimuthDeg;
    sweepWidthDeg = beamWidthDeg;
    steps = 36;
  }

  const halfSweep = sweepWidthDeg / 2;
  const points: [number, number][] = [];
  for (let step = 0; step <= steps; step++) {
    const bearing = centreBearing - halfSweep + sweepWidthDeg * (step / steps);
    const deltaFromAxis = Math.abs(((bearing - beamAzimuthDeg + 180) % 360) - 180);
    if (deltaFromAxis > beamWidthDeg / 2) continue;
    let lo = 0;
    let hi = maxRangeKm;
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

/**
 * Compute the bistatic differential-range delay (µs) for a point at
 * (targetLat, targetLon) given RX and TX positions.
 *
 * This is the inverse of the delay→locus mapping: given the current
 * dead-reckoned icon position, compute which bistatic locus it sits on.
 * Using this as the delayUs input to buildBistaticArc ensures the resulting
 * arc passes through the aircraft icon regardless of how stale the last
 * measured delay_us is — matching the test-radar behaviour where
 * aircraftPos = arcMidpoint(arc).
 */
export function computeBistaticDelayUs(
  targetLat: number, targetLon: number,
  rxLat: number, rxLon: number,
  txLat: number, txLon: number,
): number {
  const cosLat = Math.max(0.1, Math.cos(((rxLat + txLat) / 2 * Math.PI) / 180));
  const txEastKm  = (txLon - rxLon) * 111.32 * cosLat;
  const txNorthKm = (txLat - rxLat) * 111.32;
  const baselineKm = Math.hypot(txEastKm, txNorthKm);
  const tgtEastKm  = (targetLon - rxLon) * 111.32 * cosLat;
  const tgtNorthKm = (targetLat - rxLat) * 111.32;
  const tgtRxKm  = Math.hypot(tgtEastKm, tgtNorthKm);
  const tgtTxKm  = Math.hypot(tgtEastKm - txEastKm, tgtNorthKm - txNorthKm);
  return (tgtRxKm + tgtTxKm - baselineKm) / 0.299792458;
}
