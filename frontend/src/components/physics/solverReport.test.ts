import { describe, it, expect } from "vitest";
import {
  formatKm,
  formatPct,
  funnelSegments,
  rejectReasonBars,
  consensusModeLabel,
} from "./solverReport";

describe("formatKm", () => {
  it("formats a number to 2dp with a unit suffix", () => {
    expect(formatKm(0.4)).toBe("0.40 km");
    expect(formatKm(1.714)).toBe("1.71 km");
  });

  it("renders an em-dash for null/undefined", () => {
    expect(formatKm(null)).toBe("—");
    expect(formatKm(undefined)).toBe("—");
  });
});

describe("formatPct", () => {
  it("formats to 1dp by default", () => {
    expect(formatPct(91.66)).toBe("91.7%");
  });

  it("respects a custom digit count", () => {
    expect(formatPct(91.666, 2)).toBe("91.67%");
  });

  it("renders an em-dash for null/undefined", () => {
    expect(formatPct(null)).toBe("—");
  });
});

describe("funnelSegments", () => {
  it("splits published n2/n3plus and rejected with correct percentages", () => {
    const stats = {
      published: { total: 80, n2: 55, n3plus: 25 },
      rejects: { total: 20, by_reason: {} },
    };
    const segs = funnelSegments(stats);
    expect(segs.map((s) => s.count)).toEqual([55, 25, 20]);
    // total = 100
    expect(segs[0].pct).toBeCloseTo(55, 6);
    expect(segs[1].pct).toBeCloseTo(25, 6);
    expect(segs[2].pct).toBeCloseTo(20, 6);
    expect(segs.map((s) => s.color)).toEqual(["#38bdf8", "#a78bfa", "#f43f5e"]);
  });

  it("returns 0 percentages (not NaN) when there are no attempts at all", () => {
    const stats = {
      published: { total: 0, n2: 0, n3plus: 0 },
      rejects: { total: 0, by_reason: {} },
    };
    const segs = funnelSegments(stats);
    expect(segs.every((s) => s.pct === 0)).toBe(true);
  });
});

describe("rejectReasonBars", () => {
  it("sorts reasons descending by count", () => {
    const bars = rejectReasonBars({ beam: 20, displacement: 10, rms_delay: 9 });
    expect(bars.map((b) => b.reason)).toEqual(["beam", "displacement", "rms_delay"]);
    expect(bars.map((b) => b.count)).toEqual([20, 10, 9]);
  });

  it("scales pct relative to the largest reason", () => {
    const bars = rejectReasonBars({ beam: 20, displacement: 10 });
    expect(bars[0].pct).toBe(100);
    expect(bars[1].pct).toBe(50);
  });

  it("returns an empty array for no rejects (no division errors)", () => {
    expect(rejectReasonBars({})).toEqual([]);
  });
});

describe("consensusModeLabel", () => {
  it("maps known modes", () => {
    expect(consensusModeLabel("active")).toBe("Active");
    expect(consensusModeLabel("shadow")).toBe("Shadow");
    expect(consensusModeLabel("off")).toBe("Off");
  });

  it("falls back to Off for unknown values", () => {
    expect(consensusModeLabel("weird")).toBe("Off");
  });
});
