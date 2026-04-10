import { VIEWPORT_PAD_DEG } from "./constants";

export function getAircraftAnchorPoint(ac) {
  if (ac?.lat != null && ac?.lon != null) {
    return [ac.lat, ac.lon];
  }
  if (Array.isArray(ac?.ambiguity_arc) && ac.ambiguity_arc.length) {
    return ac.ambiguity_arc[Math.floor(ac.ambiguity_arc.length / 2)];
  }
  return null;
}

export function getAircraftGeometryPoints(ac) {
  if (Array.isArray(ac?.ambiguity_arc) && ac.ambiguity_arc.length >= 2) {
    return ac.ambiguity_arc;
  }
  const anchor = getAircraftAnchorPoint(ac);
  return anchor ? [anchor] : [];
}

export function isAircraftInViewport(ac, viewport, pad = VIEWPORT_PAD_DEG) {
  const points = getAircraftGeometryPoints(ac);
  if (!points.length) return false;
  return points.some(([lat, lon]) => isPointInViewport(lat, lon, viewport, pad));
}

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
    const selected = aircraft.find((ac) => ac.hex === selectedHex);
    return selected ? getAircraftGeometryPoints(selected) : [];
  }

  const validAircraft = aircraft
    .map((ac) => ({ ac, anchor: getAircraftAnchorPoint(ac) }))
    .filter(({ anchor }) => Boolean(anchor));
  if (validAircraft.length > 0) {
    // Fit to ALL aircraft positions so the user sees every marker on the map.
    return validAircraft.flatMap(({ ac }) => getAircraftGeometryPoints(ac));
  }

  return nodes
    .filter((n) => n.rx_lat && n.rx_lon)
    .map((n) => [n.rx_lat, n.rx_lon]);
}

/**
 * Yagi antenna beam sector for passive radar coverage.
 *
 * The detection zone of a single node is modelled as a pie-slice sector
 * centred on the receiver (RX), pointing at `beamAzimuthDeg` (degrees from
 * north, clockwise) with a total angular spread of `beamWidthDeg`.
 *
 * In practice the Yagi points broadside — perpendicular to the TX-RX
 * baseline — to maximise coverage of aircraft transiting the bistatic zone.
 * `beamAzimuthDeg` is already the correct perpendicular bearing supplied by
 * the analytics API; no extra rotation is needed here.
 */
function _geoOffset(lat, lon, bearingDeg, distKm) {
  const R = 6371; // Earth radius km
  const d = distKm / R;
  const latR = lat * Math.PI / 180;
  const lonR = lon * Math.PI / 180;
  const bearR = bearingDeg * Math.PI / 180;
  const lat2 = Math.asin(
    Math.sin(latR) * Math.cos(d) + Math.cos(latR) * Math.sin(d) * Math.cos(bearR),
  );
  const lon2 = lonR + Math.atan2(
    Math.sin(bearR) * Math.sin(d) * Math.cos(latR),
    Math.cos(d) - Math.sin(latR) * Math.sin(lat2),
  );
  return [lat2 * 180 / Math.PI, lon2 * 180 / Math.PI];
}

export function yagiSectorPositions(rxLat, rxLon, txLat, txLon, beamAzimuthDeg, beamWidthDeg, maxRangeKm) {
  // Fallback: if beam azimuth is not provided, compute perpendicular to the
  // RX→TX baseline (same convention used by the backend).
  let azimuth = beamAzimuthDeg;
  if (azimuth == null || Number.isNaN(azimuth)) {
    const cosLat = Math.cos(((rxLat + txLat) / 2) * (Math.PI / 180));
    const dx = (txLon - rxLon) * cosLat;
    const dy = txLat - rxLat;
    azimuth = (Math.atan2(dx, dy) * 180 / Math.PI + 90 + 360) % 360;
  }
  const halfWidth = (beamWidthDeg ?? 42) / 2;
  const range = maxRangeKm ?? 50;
  const steps = 32;
  const points = [[rxLat, rxLon]];
  for (let i = 0; i <= steps; i++) {
    const bearing = azimuth - halfWidth + (beamWidthDeg ?? 42) * (i / steps);
    points.push(_geoOffset(rxLat, rxLon, bearing, range));
  }
  points.push([rxLat, rxLon]);
  return points;
}

/**
 * @deprecated Use yagiSectorPositions instead.
 * Kept for reference — the bistatic ellipse no longer reflects the actual
 * Yagi beam pattern used in the field.
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
