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

export function beamConePositions(lat, lon, azimuthDeg, beamWidthDeg, rangeKm) {
  const R = 6371;
  const startBearing = azimuthDeg - beamWidthDeg / 2;
  const endBearing = azimuthDeg + beamWidthDeg / 2;
  const points = [[lat, lon]];
  const steps = 30;
  for (let i = 0; i <= steps; i++) {
    const bearing = startBearing + (endBearing - startBearing) * (i / steps);
    const bRad = (bearing * Math.PI) / 180;
    const d = rangeKm / R;
    const lat1 = (lat * Math.PI) / 180;
    const lon1 = (lon * Math.PI) / 180;
    const lat2 = Math.asin(
      Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(bRad)
    );
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(bRad) * Math.sin(d) * Math.cos(lat1),
        Math.cos(d) - Math.sin(lat1) * Math.sin(lat2)
      );
    points.push([(lat2 * 180) / Math.PI, (lon2 * 180) / Math.PI]);
  }
  points.push([lat, lon]);
  return points;
}
