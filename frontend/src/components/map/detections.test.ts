import { describe, it, expect } from "vitest";
import { updateDetections, detectingNodeIdsFor } from "./detections";

const TTL = 5000;

describe("updateDetections", () => {
  it("records per-aircraft signals keyed on ground_truth_hex when present", () => {
    const det: Record<string, number> = {};
    updateDetections(det, [
      { hex: "mn1234", ground_truth_hex: "abcdef", node_id: "n1",
        contributing_node_ids: ["n2", "n3"] },
    ], null, 1000, TTL);
    expect(det).toEqual({
      "abcdef|n1": 1000,
      "abcdef|n2": 1000,
      "abcdef|n3": 1000,
    });
  });

  it("unions the detecting_nodes feed key with per-aircraft signals", () => {
    const det: Record<string, number> = {};
    updateDetections(
      det,
      [{ hex: "abcdef", node_id: "n1" }],
      { abcdef: ["n1", "n4"], other1: ["n9"] },
      1000,
      TTL,
    );
    expect(Object.keys(det).sort()).toEqual([
      "abcdef|n1", "abcdef|n4", "other1|n9",
    ]);
  });

  it("prunes entries older than the TTL", () => {
    const det: Record<string, number> = { "old1|n1": 0 };
    updateDetections(det, [], { fresh1: ["n2"] }, TTL + 1, TTL);
    expect(det["old1|n1"]).toBeUndefined();
    expect(det["fresh1|n2"]).toBe(TTL + 1);
  });

  it("tolerates a missing or malformed detecting_nodes payload", () => {
    const det: Record<string, number> = {};
    updateDetections(det, [], undefined, 1000, TTL);
    updateDetections(det, [], { bad1: "not-a-list" } as never, 1000, TTL);
    expect(det).toEqual({});
  });
});

describe("detectingNodeIdsFor", () => {
  it("returns sorted node ids within the TTL for the hex", () => {
    const det = {
      "abcdef|n2": 900,
      "abcdef|n1": 1000,
      "abcdef|n3": 1000 - TTL - 1, // expired
      "other1|n4": 1000,           // different aircraft
    };
    expect(detectingNodeIdsFor(det, "abcdef", 1000, TTL))
      .toEqual(["n1", "n2"]);
  });

  it("does not match hexes sharing a prefix", () => {
    const det = { "abcd|n1": 1000, "abcdef|n2": 1000 };
    expect(detectingNodeIdsFor(det, "abcd", 1000, TTL)).toEqual(["n1"]);
  });
});
