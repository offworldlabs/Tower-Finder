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
function differentialAt(node: NodeGeometry, lat: number, lon: number): number {
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
  return g + gTx - baseline;
}

describe("buildBistaticArc (2D, no altitude)", () => {
  it("returns a locus whose 2D differential matches the measured delay", () => {
    const delayUs = 46.2;
    const arc = buildBistaticArc(delayUs, NODE);
    expect(arc).not.toBeNull();
    expect(arc!.length).toBeGreaterThanOrEqual(2);
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      expect(differentialAt(NODE, lat, lon)).toBeCloseTo(target, 2);
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

  it("ignores altitude-ish fields on the node — the locus is unchanged", () => {
    // rx_alt_m / tx_alt_m stay on NodeGeometry for other consumers (see
    // hooks.ts / types.ts) but no longer feed this builder's geometry
    // (2026-08 direction: arcs are altitude-agnostic by design).
    const delayUs = 46.2;
    const plain = buildBistaticArc(delayUs, NODE);
    const withAlt = buildBistaticArc(delayUs, {
      ...NODE, rx_alt_m: 300, tx_alt_m: 9500,
    });
    expect(withAlt).toEqual(plain);
  });
});
