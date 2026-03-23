import { VIEWPORT_PAD_DEG, FOCUS_CLUSTER_LIMIT } from "./constants";

export function buildViewportSnapshot(bounds) {
  return {
    north: bounds.getNorth(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    west: bounds.getWest(),
  };
}

export function isPointInViewport(lat, lon, viewport, pad = VIEWPORT_PAD_DEG) {
  if (!viewport || lat == null || lon == null) return true;
  return (
    lat >= viewport.south - pad &&
    lat <= viewport.north + pad &&
    lon >= viewport.west - pad &&
    lon <= viewport.east + pad
  );
}

export function getFocusPoints(aircraft, nodes, selectedHex) {
  if (selectedHex) {
    const selected = aircraft.find((ac) => ac.hex === selectedHex && ac.lat && ac.lon);
    return selected ? [[selected.lat, selected.lon]] : [];
  }

  const validAircraft = aircraft.filter((ac) => ac.lat && ac.lon);
  if (validAircraft.length > 0) {
    let bestCenter = validAircraft[0];
    let bestScore = -1;

    for (const center of validAircraft) {
      let score = 0;
      for (const ac of validAircraft) {
        if (Math.abs(ac.lat - center.lat) <= 4 && Math.abs(ac.lon - center.lon) <= 6) {
          score += 1;
        }
      }
      if (score > bestScore) {
        bestScore = score;
        bestCenter = center;
      }
    }

    return validAircraft
      .slice()
      .sort((a, b) => {
        const distA = Math.pow(a.lat - bestCenter.lat, 2) + Math.pow(a.lon - bestCenter.lon, 2);
        const distB = Math.pow(b.lat - bestCenter.lat, 2) + Math.pow(b.lon - bestCenter.lon, 2);
        return distA - distB;
      })
      .slice(0, FOCUS_CLUSTER_LIMIT)
      .map((ac) => [ac.lat, ac.lon]);
  }

  return nodes
    .filter((n) => n.rx_lat && n.rx_lon)
    .map((n) => [n.rx_lat, n.rx_lon]);
}

/**
 * Bistatic oval (ellipse) for passive radar coverage.
 *
 * The detection zone of a bistatic RX/TX pair is an ellipse whose foci are
 * the receiver and the transmitter.  Every point on the boundary satisfies
 * d_rx + d_tx = L + maxRangeKm  (where L = baseline distance).
 *
 * Semi-major  a = (L + maxRangeKm) / 2
 * Semi-minor  b = sqrt(a² − (L/2)²)
 * Center      = midpoint of RX–TX
 * Tilt        = bearing along the RX→TX axis
 */
export function bistaticOvalPositions(rxLat, rxLon, txLat, txLon, maxRangeKm) {
  const cosLat = Math.cos(((rxLat + txLat) / 2) * (Math.PI / 180));
  const dx = (txLon - rxLon) * cosLat * 111.32; // km east
  const dy = (txLat - rxLat) * 111.32;          // km north
  const L  = Math.sqrt(dx * dx + dy * dy);       // baseline km

  const a = (L + maxRangeKm) / 2;
  const c = L / 2;
  const b = Math.sqrt(Math.max(0, a * a - c * c));

  // Tilt: angle of major axis from north (RX→TX bearing)
  const tiltRad = Math.atan2(dx, dy);

  const centerLat = (rxLat + txLat) / 2;
  const centerLon = (rxLon + txLon) / 2;

  const points = [];
  const steps = 64;
  for (let i = 0; i <= steps; i++) {
    const theta = (2 * Math.PI * i) / steps;
    const cosT = Math.cos(theta);
    const sinT = Math.sin(theta);
    // Rotate ellipse by tilt angle
    const localNorth = a * cosT * Math.cos(tiltRad) - b * sinT * Math.sin(tiltRad);
    const localEast  = a * cosT * Math.sin(tiltRad) + b * sinT * Math.cos(tiltRad);
    const lat = centerLat + localNorth / 111.32;
    const lon = centerLon + localEast  / (111.32 * cosLat);
    points.push([lat, lon]);
  }
  return points;
}
