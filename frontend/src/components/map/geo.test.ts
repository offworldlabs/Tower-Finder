import { describe, it, expect } from "vitest";
import { haversineDistanceKm, bearingDeg, isInBeam } from "./geo";

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
