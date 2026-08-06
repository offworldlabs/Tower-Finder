import { describe, it, expect } from "vitest";
import { buildBistaticArc, type NodeGeometry } from "./bistaticArc";

const C_KM_PER_US = 0.299792458;

// RX/TX ~18.5 km apart, north-pointing 42° beam wedge.
const NODE: NodeGeometry = {
  rx_lat: 34.0,
  rx_lon: -82.0,
  tx_lat: 34.0,
  tx_lon: -81.8,
  beam_azimuth_deg: 0,
  beam_width_deg: 42,
  max_range_km: 60,
  max_bistatic_range_km: null,
};

/**
 * Recompute the builder's own ENU differential for a returned (lat, lon)
 * point — mirrors the internal projection (equirect; tx offsets use the
 * midpoint latitude, point inversion uses the RX latitude per enuToLla).
 */
function differentialsAt(
  node: NodeGeometry,
  lat: number,
  lon: number,
  altKm: number | null,
): { d2: number; d3: number } {
  const rxLat = node.rx_lat_real ?? node.rx_lat;
  const rxLon = node.rx_lon_real ?? node.rx_lon;
  const cosMid = Math.max(
    0.1,
    Math.cos((((rxLat + node.tx_lat) / 2) * Math.PI) / 180),
  );
  const txEast = (node.tx_lon - rxLon) * 111.32 * cosMid;
  const txNorth = (node.tx_lat - rxLat) * 111.32;
  const baseline = Math.hypot(txEast, txNorth);
  const cosRx = Math.max(0.1, Math.cos((rxLat * Math.PI) / 180));
  const east = (lon - rxLon) * 111.32 * cosRx;
  const north = (lat - rxLat) * 111.32;
  const g = Math.hypot(east, north);
  const gTx = Math.hypot(east - txEast, north - txNorth);
  const d2 = g + gTx - baseline;
  if (altKm == null) return { d2, d3: d2 };
  const rxAltKm = Number(node.rx_alt_m ?? 0) / 1000;
  const txAltKm = Number(node.tx_alt_m ?? 0) / 1000;
  const baseline3d = Math.hypot(baseline, txAltKm - rxAltKm);
  const d3 =
    Math.hypot(g, altKm - rxAltKm) +
    Math.hypot(gTx, altKm - txAltKm) -
    baseline3d;
  return { d2, d3 };
}

describe("buildBistaticArc (2D, no altitude)", () => {
  it("returns a locus whose 2D differential matches the measured delay", () => {
    const delayUs = 46.2;
    const arc = buildBistaticArc(delayUs, NODE);
    expect(arc).not.toBeNull();
    expect(arc!.length).toBeGreaterThanOrEqual(2);
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      const { d2 } = differentialsAt(NODE, lat, lon, null);
      expect(d2).toBeCloseTo(target, 2);
    }
  });

  it("rejects non-positive delays and missing geometry", () => {
    expect(buildBistaticArc(0, NODE)).toBeNull();
    expect(buildBistaticArc(-5, NODE)).toBeNull();
    expect(
      buildBistaticArc(46.2, { ...NODE, tx_lat: null as any }),
    ).toBeNull();
  });

  it("returns null when the delay exceeds a declared differential limit", () => {
    const node = { ...NODE, max_bistatic_range_km: 10 };
    // 46.2 µs → 13.85 km differential > 10 km limit
    expect(buildBistaticArc(46.2, node)).toBeNull();
  });
});

describe("buildBistaticArc altitude correction", () => {
  it("altitude 0 with zero node altitudes reproduces the 2D arc exactly", () => {
    const arc2d = buildBistaticArc(46.2, NODE);
    const arc3d = buildBistaticArc(46.2, NODE, 0);
    expect(arc3d).toEqual(arc2d);
  });

  it("null / undefined altitude keeps the 2D behavior", () => {
    const arc2d = buildBistaticArc(46.2, NODE);
    expect(buildBistaticArc(46.2, NODE, null)).toEqual(arc2d);
    expect(buildBistaticArc(46.2, NODE, undefined)).toEqual(arc2d);
  });

  it("matches the 3D differential on the returned ground locus", () => {
    const delayUs = 46.2;
    const altM = 10_000; // FL330-ish
    const arc = buildBistaticArc(delayUs, NODE, altM);
    expect(arc).not.toBeNull();
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      const { d3 } = differentialsAt(NODE, lat, lon, altM / 1000);
      expect(d3).toBeCloseTo(target, 2);
    }
  });

  it("pulls the ground locus inward versus the 2D arc (slant vs ground range)", () => {
    const delayUs = 46.2;
    const arc = buildBistaticArc(delayUs, NODE, 10_000);
    expect(arc).not.toBeNull();
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      // At the 3D locus's ground point, the pure-2D differential must be
      // strictly below the measured one — the altitude legs make up the rest.
      const { d2 } = differentialsAt(NODE, lat, lon, null);
      expect(d2).toBeLessThan(target);
    }
  });

  it("uses node RX/TX altitudes when present", () => {
    const node: NodeGeometry = { ...NODE, rx_alt_m: 300, tx_alt_m: 250 };
    const delayUs = 46.2;
    const altM = 10_000;
    const arc = buildBistaticArc(delayUs, node, altM);
    expect(arc).not.toBeNull();
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      const { d3 } = differentialsAt(node, lat, lon, altM / 1000);
      expect(d3).toBeCloseTo(target, 2);
    }
    // And it differs from the sea-level-node solution.
    expect(arc).not.toEqual(buildBistaticArc(delayUs, NODE, altM));
  });

  it("returns null when the delay is inconsistent with the altitude", () => {
    // ~2 km differential can't be reached by a target 10 km up: even the
    // point directly above the RX implies a far larger 3D differential.
    const delayUs = 2 / C_KM_PER_US;
    expect(buildBistaticArc(delayUs, NODE)).not.toBeNull(); // fine in 2D
    expect(buildBistaticArc(delayUs, NODE, 10_000)).toBeNull();
  });
});
