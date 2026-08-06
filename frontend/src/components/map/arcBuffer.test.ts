import { describe, it, expect } from "vitest";
import { arcBufferKey, upsertArcEntries } from "./arcBuffer";

const ARC: [number, number][] = [
  [34.0, -82.0],
  [34.1, -82.0],
  [34.2, -82.0],
];

function mkAc(overrides = {}) {
  return {
    hex: "abc123",
    node_id: "node-1",
    ambiguity_arc: ARC,
    delay_us: 46.2,
    alt_baro: 30000,
    doppler_hz: 12,
    target_class: "aircraft",
    ...overrides,
  };
}

describe("arcBufferKey", () => {
  it("keys by hex + node + delay quantized to 0.1 µs", () => {
    expect(arcBufferKey("abc123", "node-1", 46.2, 1000)).toBe(
      "abc123-node-1-d462",
    );
  });

  it("gives the same key for delays within the same 0.1 µs bin", () => {
    const a = arcBufferKey("abc123", "node-1", 46.21, 1000);
    const b = arcBufferKey("abc123", "node-1", 46.24, 2000);
    expect(a).toBe(b);
  });

  it("gives different keys for delays in different bins", () => {
    const a = arcBufferKey("abc123", "node-1", 46.23, 1000);
    const b = arcBufferKey("abc123", "node-1", 46.26, 1000);
    expect(a).not.toBe(b);
  });

  it("falls back to timestamp keying when delay is missing", () => {
    expect(arcBufferKey("abc123", "node-1", null, 1234)).toBe(
      "abc123-node-1-t1234",
    );
    expect(arcBufferKey("abc123", "node-1", undefined, 1234)).toBe(
      "abc123-node-1-t1234",
    );
    expect(arcBufferKey("abc123", "node-1", NaN, 1234)).toBe(
      "abc123-node-1-t1234",
    );
  });

  it("separates aircraft and nodes", () => {
    const keys = new Set([
      arcBufferKey("abc123", "node-1", 46.2, 1000),
      arcBufferKey("abc123", "node-2", 46.2, 1000),
      arcBufferKey("def456", "node-1", 46.2, 1000),
    ]);
    expect(keys.size).toBe(3);
  });
});

describe("upsertArcEntries", () => {
  const MAX_AGE = 5000;

  it("lays a new entry for a new (hex, node, delay)", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc()], 1000, MAX_AGE);
    const keys = Object.keys(buf);
    expect(keys).toHaveLength(1);
    expect(buf[keys[0]]).toMatchObject({
      hex: "abc123",
      node_id: "node-1",
      delay_us: 46.2,
      alt_baro: 30000,
      doppler_hz: 12,
      target_class: "aircraft",
      ts: 1000,
    });
    expect(buf[keys[0]].ambiguity_arc).toBe(ARC);
  });

  it("refreshes ONE entry's fade clock when the same measurement is re-ingested", () => {
    const buf = {};
    // Five 1 Hz ingests of an unchanged measurement — the exact staging
    // failure case that used to stack five concurrent parallel strokes.
    for (let i = 0; i < 5; i++) {
      upsertArcEntries(buf, [mkAc({ doppler_hz: 12 + i })], 1000 + i * 1000, MAX_AGE);
    }
    const keys = Object.keys(buf);
    expect(keys).toHaveLength(1);
    expect(buf[keys[0]].ts).toBe(5000); // fade clock refreshed to last ingest
    expect(buf[keys[0]].doppler_hz).toBe(16); // style fields refreshed
  });

  it("refreshes target_class and alt_baro on re-ingest", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc()], 1000, MAX_AGE);
    upsertArcEntries(
      buf,
      [mkAc({ target_class: "drone", alt_baro: 31000 })],
      2000,
      MAX_AGE,
    );
    const entry = buf[Object.keys(buf)[0]];
    expect(entry.target_class).toBe("drone");
    expect(entry.alt_baro).toBe(31000);
  });

  it("lays a new entry when the delay changes (genuine afterglow trail)", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc({ delay_us: 46.2 })], 1000, MAX_AGE);
    upsertArcEntries(buf, [mkAc({ delay_us: 45.8 })], 2000, MAX_AGE);
    expect(Object.keys(buf)).toHaveLength(2);
    const tss = Object.values(buf).map((e: any) => e.ts).sort();
    expect(tss).toEqual([1000, 2000]); // old entry keeps fading on its own clock
  });

  it("skips on same-millisecond collision for timestamp-keyed (no-delay) entries", () => {
    const buf = {};
    const first = mkAc({ delay_us: null });
    const second = mkAc({ delay_us: null, doppler_hz: 99 });
    upsertArcEntries(buf, [first, second], 1000, MAX_AGE);
    const keys = Object.keys(buf);
    expect(keys).toHaveLength(1);
    // The existing entry is kept, not overwritten.
    expect(buf[keys[0]].doppler_hz).toBe(12);
  });

  it("stacks timestamp-keyed (no-delay) entries per ingest — legacy behavior", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc({ delay_us: null })], 1000, MAX_AGE);
    upsertArcEntries(buf, [mkAc({ delay_us: null })], 2000, MAX_AGE);
    expect(Object.keys(buf)).toHaveLength(2);
  });

  it("prunes entries older than maxAgeMs", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc({ delay_us: 46.2 })], 1000, MAX_AGE);
    upsertArcEntries(buf, [mkAc({ delay_us: 40.0, hex: "def456" })], 7000, MAX_AGE);
    const keys = Object.keys(buf);
    expect(keys).toHaveLength(1);
    expect(buf[keys[0]].hex).toBe("def456");
  });

  it("a refreshed entry survives pruning that would have removed the original", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc()], 1000, MAX_AGE);
    upsertArcEntries(buf, [mkAc()], 4000, MAX_AGE); // refresh at t=4s
    upsertArcEntries(buf, [], 8000, MAX_AGE); // t=8s: 7s after lay, 4s after refresh
    expect(Object.keys(buf)).toHaveLength(1);
  });

  it("ignores aircraft without an arc or node_id", () => {
    const buf = {};
    upsertArcEntries(
      buf,
      [
        mkAc({ ambiguity_arc: null }),
        mkAc({ ambiguity_arc: [[34.0, -82.0]] }), // < 2 points
        mkAc({ node_id: null }),
      ],
      1000,
      MAX_AGE,
    );
    expect(Object.keys(buf)).toHaveLength(0);
  });

  it("normalises non-finite alt_baro to null", () => {
    const buf = {};
    upsertArcEntries(buf, [mkAc({ alt_baro: "ground" })], 1000, MAX_AGE);
    expect(buf[Object.keys(buf)[0]].alt_baro).toBe(null);
  });
});
