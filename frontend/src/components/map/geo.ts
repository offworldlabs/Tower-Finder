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
    // When focusing a selected aircraft, return ONLY the anchor point so
    // FitBounds takes the setView(anchor, currentZoom) branch — fitting the
    // bounds to the full ambiguity arc geometry instead zooms the camera
    // down to street level on a ~2 km arc, leaving the aircraft barely
    // visible. The anchor IS the aircraft position (arc midpoint for
    // arc-only tracks), which is what the user actually wants to centre.
    const selected = aircraft.find((ac) => ac.hex === selectedHex);
    if (!selected) return [];
    const anchor = getAircraftAnchorPoint(selected);
    return anchor ? [anchor] : [];
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

/**
 * How far a bistatic node sees at `psiDeg` off its RX→TX baseline.
 *
 * A node is bounded by a *differential* range — R_tx + R_rx − L — so its
 * footprint is an ellipse with foci at TX and RX, whose polar radius from the
 * RX focus is
 *
 *     r(ψ) = Δ(Δ + 2L) / (2·[(L + Δ) − L·cos ψ])
 *
 *     ψ = 0    (toward TX)     r = Δ/2 + L
 *     ψ = 180° (away from TX)  r = Δ/2      — independent of the baseline
 *
 * Mirrors bistatic_range_limit_km in retina_analytics/constants.py, which is
 * what the association gate uses.  Drawing a circle of radius Δ instead
 * overstates coverage by 2× behind the receiver and understates it toward a
 * distant tower, so the map would disagree with the gate in both directions.
 */
export function bistaticRangeLimitKm(psiDeg, baselineKm, maxBistaticKm) {
  const d = maxBistaticKm;
  const L = Math.max(baselineKm, 0);
  const denom = 2 * ((L + d) - L * Math.cos(psiDeg * Math.PI / 180));
  if (denom <= 0) return 0;
  return d * (d + 2 * L) / denom;
}

export function yagiSectorPositions(
  rxLat, rxLon, txLat, txLon, beamAzimuthDeg, beamWidthDeg, maxRangeKm,
  maxBistaticRangeKm = null,
) {
  // Fallback: if beam azimuth is not provided, compute perpendicular to the
  // RX→TX baseline (same convention used by the backend).
  let azimuth = beamAzimuthDeg;
  if (azimuth == null || Number.isNaN(azimuth)) {
    const cosLat = Math.cos(((rxLat + txLat) / 2) * (Math.PI / 180));
    const dx = (txLon - rxLon) * cosLat;
    const dy = txLat - rxLat;
    azimuth = (Math.atan2(dx, dy) * 180 / Math.PI + 90 + 360) % 360;
  }
  const width = beamWidthDeg ?? 42;
  const halfWidth = width / 2;
  const range = maxRangeKm ?? 50;

  // With a declared differential limit the outer boundary is an elliptical arc,
  // so the radius is recomputed per step instead of held constant.
  const useBistatic = maxBistaticRangeKm != null && Number.isFinite(maxBistaticRangeKm);
  const baselineKm = useBistatic
    ? haversineDistanceKm(rxLat, rxLon, txLat, txLon)
    : 0;
  const bearingToTx = useBistatic ? _bearingDeg(rxLat, rxLon, txLat, txLon) : 0;

  // The elliptical edge curves faster than a circular one, so it needs more
  // vertices to stay smooth over a wide sector.
  const steps = useBistatic ? 64 : 32;
  const points = [[rxLat, rxLon]];
  for (let i = 0; i <= steps; i++) {
    const bearing = azimuth - halfWidth + width * (i / steps);
    let r = range;
    if (useBistatic) {
      const psi = Math.abs(((bearing - bearingToTx + 180) % 360 + 360) % 360 - 180);
      r = bistaticRangeLimitKm(psi, baselineKm, maxBistaticRangeKm);
    }
    points.push(_geoOffset(rxLat, rxLon, bearing, r));
  }
  points.push([rxLat, rxLon]);
  return points;
}

function _bearingDeg(lat1, lon1, lat2, lon2) {
  const lat1r = lat1 * Math.PI / 180;
  const lat2r = lat2 * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const x = Math.sin(dLon) * Math.cos(lat2r);
  const y = Math.cos(lat1r) * Math.sin(lat2r)
    - Math.sin(lat1r) * Math.cos(lat2r) * Math.cos(dLon);
  return ((Math.atan2(x, y) * 180 / Math.PI) % 360 + 360) % 360;
}

const EARTH_RADIUS_KM = 6371;

export function haversineDistanceKm(lat1, lon1, lat2, lon2) {
  const phi1 = lat1 * Math.PI / 180;
  const phi2 = lat2 * Math.PI / 180;
  const dPhi = (lat2 - lat1) * Math.PI / 180;
  const dLambda = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  return EARTH_RADIUS_KM * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function bearingDeg(fromLat, fromLon, toLat, toLon) {
  const phi1 = fromLat * Math.PI / 180;
  const phi2 = toLat * Math.PI / 180;
  const dLambda = (toLon - fromLon) * Math.PI / 180;
  const y = Math.sin(dLambda) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

export function isInBeam(rxLat, rxLon, azimuthDeg, beamWidthDeg, maxRangeKm, acLat, acLon) {
  if (haversineDistanceKm(rxLat, rxLon, acLat, acLon) > maxRangeKm) return false;
  const bearing = bearingDeg(rxLat, rxLon, acLat, acLon);
  // +540 (= 360 + 180) keeps the operand to % positive — JS's % returns the
  // sign of the dividend, so (bearing - azimuth + 180) % 360 - 180 breaks
  // when bearing - azimuth < -180.  Normalising to [-180, 180] this way is
  // safe for any input.
  const delta = ((bearing - azimuthDeg + 540) % 360) - 180;
  return Math.abs(delta) <= beamWidthDeg / 2;
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

/** True when both coordinates are usable.  null/undefined and the (0, 0)
 *  broken-config sentinel are invalid, but a legitimate 0 on a single axis
 *  (equator / prime meridian) is not — the widespread `!lat || !lon` form
 *  silently dropped those. */
export function validLatLon(lat: number | null | undefined, lon: number | null | undefined): boolean {
  return lat != null && lon != null && !(lat === 0 && lon === 0);
}
