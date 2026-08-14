import { describe, it, expect } from "vitest";
import {
  haversineDistanceKm,
  bearingDeg,
  isInBeam,
  bistaticRangeLimitKm,
  nearestPointOnPolyline,
  yagiSectorPositions,
} from "./geo";

describe("haversineDistanceKm", () => {
  it("is zero for the same point", () => {
    expect(haversineDistanceKm(0, 0, 0, 0)).toBe(0);
  });

  it("returns ~111.2 km for 1° of latitude at the equator", () => {
    expect(haversineDistanceKm(0, 0, 1, 0)).toBeCloseTo(111.19, 1);
  });

  it("is symmetric", () => {
    const a = haversineDistanceKm(51.5, -0.13, 48.85, 2.35); // London → Paris
    const b = haversineDistanceKm(48.85, 2.35, 51.5, -0.13);
    expect(a).toBeCloseTo(b, 6);
  });
});

describe("bearingDeg", () => {
  it("is 0 for due north", () => {
    expect(bearingDeg(0, 0, 1, 0)).toBeCloseTo(0, 6);
  });

  it("is 90 for due east at the equator", () => {
    expect(bearingDeg(0, 0, 0, 1)).toBeCloseTo(90, 6);
  });

  it("is 180 for due south", () => {
    expect(bearingDeg(0, 0, -1, 0)).toBeCloseTo(180, 6);
  });

  it("is 270 for due west at the equator", () => {
    expect(bearingDeg(0, 0, 0, -1)).toBeCloseTo(270, 6);
  });

  it("normalises to [0, 360)", () => {
    // Any input should produce a value in [0, 360); never negative.
    const b = bearingDeg(0, 0, -0.5, -0.5); // somewhere south-west
    expect(b).toBeGreaterThanOrEqual(0);
    expect(b).toBeLessThan(360);
  });
});

describe("isInBeam", () => {
  const RX_LAT = 0;
  const RX_LON = 0;
  const MAX_RANGE_KM = 200;

  it("includes a target on the boresight within range", () => {
    // Beam pointing north, half-width 30°. Target due north at ~111 km.
    expect(isInBeam(RX_LAT, RX_LON, 0, 60, MAX_RANGE_KM, 1, 0)).toBe(true);
  });

  it("includes a target just inside the half-width", () => {
    // Beam pointing north, half-width 30°. Target at ~14° east of north.
    expect(isInBeam(RX_LAT, RX_LON, 0, 60, MAX_RANGE_KM, 1, 0.25)).toBe(true);
  });

  it("excludes a target outside the half-width", () => {
    // Beam pointing north, half-width 30°. Target at ~45° east of north.
    expect(isInBeam(RX_LAT, RX_LON, 0, 60, MAX_RANGE_KM, 0.5, 0.5)).toBe(false);
  });

  it("excludes a target beyond max range even on the boresight", () => {
    // Target due north at ~222 km, beyond max range of 200 km.
    expect(isInBeam(RX_LAT, RX_LON, 0, 60, MAX_RANGE_KM, 2, 0)).toBe(false);
  });

  it("handles azimuth wraparound near 0/360", () => {
    // Beam pointing 350° (just west of north), half-width 30°.
    // Target due north (bearing 0°) — delta from 350° is 10°, well inside.
    expect(isInBeam(RX_LAT, RX_LON, 350, 60, MAX_RANGE_KM, 1, 0)).toBe(true);
  });

  it("handles azimuth wraparound from the other side", () => {
    // Beam pointing 10° (just east of north), half-width 30°.
    // Target just west of north (bearing ~354°) — delta is ~16°, inside.
    expect(isInBeam(RX_LAT, RX_LON, 10, 60, MAX_RANGE_KM, 1, -0.1)).toBe(true);
  });

  it("excludes a target on the opposite side of the wraparound", () => {
    // Beam pointing 10°, half-width 30°. Target at bearing ~315° (NW),
    // delta ≈ -55°, well outside.
    expect(isInBeam(RX_LAT, RX_LON, 10, 60, MAX_RANGE_KM, 0.5, -0.5)).toBe(false);
  });
});

describe("bistaticRangeLimitKm", () => {
  const DELTA = 60;

  it("gives Δ/2 directly away from the transmitter, whatever the baseline", () => {
    // The case a circle of radius Δ gets most wrong: 30 km of real coverage
    // against 60 km assumed.
    for (const baseline of [5, 25, 43, 100]) {
      expect(bistaticRangeLimitKm(180, baseline, DELTA)).toBeCloseTo(30, 6);
    }
  });

  it("gives Δ/2 + L toward the transmitter, which can exceed Δ", () => {
    expect(bistaticRangeLimitKm(0, 25, DELTA)).toBeCloseTo(55, 6);
    // A tower 43 km out genuinely reaches past 60 km.
    expect(bistaticRangeLimitKm(0, 43, DELTA)).toBeCloseTo(73, 6);
  });

  it("collapses to a circle of radius Δ/2 with no baseline", () => {
    for (const psi of [0, 45, 90, 180]) {
      expect(bistaticRangeLimitKm(psi, 0, DELTA)).toBeCloseTo(30, 6);
    }
  });

  it("matches the backend's bistatic_range_limit_km", () => {
    // Same formula the association gate uses; drift between them would make
    // the map disagree with what is actually being associated.
    expect(bistaticRangeLimitKm(90, 30, DELTA)).toBeCloseTo(
      (DELTA * (DELTA + 2 * 30)) / (2 * (30 + DELTA)), 6,
    );
  });
});

describe("yagiSectorPositions", () => {
  // RX at 34.85N, TX 43 km due north — a long baseline, so the two shapes
  // differ in both directions.
  const RX_LAT = 34.85, RX_LON = -82.40;
  const TX_LAT = 35.236, TX_LON = -82.40;

  function reach(points, bearingDegWanted) {
    // Distance from RX to the vertex nearest the requested bearing.
    let best = null, bestDiff = Infinity;
    for (const [lat, lon] of points.slice(1, -1)) {
      const b = bearingDeg(RX_LAT, RX_LON, lat, lon);
      const diff = Math.abs(((b - bearingDegWanted + 180) % 360 + 360) % 360 - 180);
      if (diff < bestDiff) { bestDiff = diff; best = [lat, lon]; }
    }
    return haversineDistanceKm(RX_LAT, RX_LON, best[0], best[1]);
  }

  it("keeps a constant radius when no bistatic limit is given", () => {
    const pts = yagiSectorPositions(RX_LAT, RX_LON, TX_LAT, TX_LON, 0, 180, 60);
    expect(reach(pts, 0)).toBeCloseTo(60, 0);
    expect(reach(pts, 90)).toBeCloseTo(60, 0);
  });

  it("reaches further toward the transmitter than away from it", () => {
    const pts = yagiSectorPositions(RX_LAT, RX_LON, TX_LAT, TX_LON, 0, 360, 60, 60);
    const toward = reach(pts, 0);     // due north, at the TX
    const away = reach(pts, 180);     // due south
    expect(toward).toBeCloseTo(73, 0);
    expect(away).toBeCloseTo(30, 0);
    expect(toward).toBeGreaterThan(away * 2);
  });

  it("closes the polygon at the receiver", () => {
    const pts = yagiSectorPositions(RX_LAT, RX_LON, TX_LAT, TX_LON, 0, 41, 60, 60);
    expect(pts[0]).toEqual([RX_LAT, RX_LON]);
    expect(pts[pts.length - 1]).toEqual([RX_LAT, RX_LON]);
  });
});

describe("nearestPointOnPolyline", () => {
  const LAT = 34.85, LON = -82.4;

  it("returns null for an empty or missing polyline", () => {
    expect(nearestPointOnPolyline(LAT, LON, [])).toBeNull();
    expect(nearestPointOnPolyline(LAT, LON, null)).toBeNull();
  });

  it("handles a single-point polyline", () => {
    const r = nearestPointOnPolyline(LAT, LON, [[LAT + 0.1, LON]] as [number, number][]);
    expect(r.lat).toBeCloseTo(LAT + 0.1, 10);
    expect(r.lon).toBeCloseTo(LON, 10);
    expect(r.distKm).toBeCloseTo(haversineDistanceKm(LAT, LON, LAT + 0.1, LON), 3);
  });

  it("projects onto the interior of a segment, not just vertices", () => {
    // Horizontal segment 0.1° north of the query point: the nearest point is
    // directly above the query, mid-segment — not either endpoint.
    const pts: [number, number][] = [[LAT + 0.1, LON - 0.5], [LAT + 0.1, LON + 0.5]];
    const r = nearestPointOnPolyline(LAT, LON, pts);
    expect(r.lat).toBeCloseTo(LAT + 0.1, 6);
    expect(r.lon).toBeCloseTo(LON, 6);
    expect(r.distKm).toBeCloseTo(haversineDistanceKm(LAT, LON, LAT + 0.1, LON), 2);
  });

  it("clamps to the nearest endpoint when the projection falls outside", () => {
    const pts: [number, number][] = [[LAT, LON + 0.2], [LAT, LON + 0.6]];
    const r = nearestPointOnPolyline(LAT, LON, pts);
    expect(r.lat).toBeCloseTo(LAT, 10);
    expect(r.lon).toBeCloseTo(LON + 0.2, 10);
  });

  it("is never farther than the nearest vertex", () => {
    const pts: [number, number][] = [
      [LAT + 0.3, LON - 0.4], [LAT + 0.25, LON - 0.1],
      [LAT + 0.2, LON + 0.2], [LAT + 0.35, LON + 0.5],
    ];
    const r = nearestPointOnPolyline(LAT, LON, pts);
    const vertexMin = Math.min(
      ...pts.map(([a, b]) => haversineDistanceKm(LAT, LON, a, b)),
    );
    expect(r.distKm).toBeLessThanOrEqual(vertexMin + 1e-9);
  });
});
