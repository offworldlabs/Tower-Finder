import { describe, it, expect } from "vitest";
import { applyGroundTruthFixes, pruneGroundTruthFixes, sweepStaleGroundTruthFixes } from "./groundTruthFixes";
import { groundTruthKey } from "./constants";

// Trail point layout matches the backend snapshot: [lat, lon, alt_m, ts].
const trail = (lat, lon, alt = 3048) => [[lat, lon, alt, 1785837805.9]];

const META = { "652600": { speed_ms: 148.8, heading: 217.3, object_type: "aircraft" } };

describe("applyGroundTruthFixes", () => {
  it("namespaces truth entries away from the bare hex", () => {
    const fixes: Record<string, any> = {};
    const keys = applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);

    expect(Object.keys(fixes)).toEqual([groundTruthKey("652600")]);
    expect(fixes["652600"]).toBeUndefined();
    expect(keys).toEqual(new Set([groundTruthKey("652600")]));
  });

  it("keeps the real hex on the object for labels and selection", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);

    const fix = fixes[groundTruthKey("652600")];
    expect(fix.hex).toBe("652600");
    expect(fix._key).toBe(groundTruthKey("652600"));
    expect(fix._isTruth).toBe(true);
  });

  // The regression this file exists for: a simulated radar track carries the
  // aircraft's own ICAO hex, so before namespacing the two ingests fought over
  // one store key and the marker alternated between the solved position and
  // the true one at ~1 Hz — 29.8 km apart for the arc track that surfaced it.
  it("does not disturb a radar fix carrying the same hex", () => {
    const radar = {
      hex: "652600", _key: "652600",
      lat: 34.8443, lon: -82.342,
      position_source: "single_node_ellipse_arc",
      _fixLat: 34.8443, _fixLon: -82.342, _fixTs: 500,
    };
    const fixes: Record<string, any> = { "652600": radar };

    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);

    expect(fixes["652600"]).toBe(radar);
    expect(fixes["652600"].lat).toBe(34.8443);
    expect(fixes["652600"]._isTruth).toBeUndefined();
    expect(fixes[groundTruthKey("652600")].lat).toBe(34.61);
  });

  it("preserves the dead-reckoning anchor while the fix has not moved", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 2000);

    expect(fixes[groundTruthKey("652600")]._fixTs).toBe(1000);
    expect(fixes[groundTruthKey("652600")]._updatedAt).toBe(2000);
  });

  it("re-anchors when the fix moves", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);
    applyGroundTruthFixes(fixes, { "652600": trail(34.62, -82.4996) }, META, 2000);

    expect(fixes[groundTruthKey("652600")]._fixTs).toBe(2000);
  });

  it("converts altitude to feet and speed to knots", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996, 3048) }, META, 1000);

    const fix = fixes[groundTruthKey("652600")];
    expect(fix.alt_baro).toBe(10000);
    expect(fix.gs).toBeCloseTo(289.2, 1);
  });

  it("skips hexes with an empty trail", () => {
    const fixes: Record<string, any> = {};
    const keys = applyGroundTruthFixes(fixes, { "652600": [], "218b6d": null }, META, 1000);

    expect(Object.keys(fixes)).toEqual([]);
    expect(keys.size).toBe(0);
  });
});

// Minimal trackStores bundle for tests — every store the map keeps.
function makeStores(fixes = {}, extra = {}) {
  return {
    fixes,
    smooth: {}, svgElems: {}, svgMiss: {}, latLng: {},
    trails: {}, lastTrailSample: {},
    markerRegistry: new Map(),
    ...extra,
  };
}

describe("pruneGroundTruthFixes", () => {
  it("drops truth entries the snapshot no longer vouches for, across every store", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(
      fixes, { "652600": trail(34.61, -82.4996), "218b6d": trail(34.85, -81.67) }, META, 1000,
    );
    const stores = makeStores(fixes, {
      smooth: { [groundTruthKey("652600")]: {}, [groundTruthKey("218b6d")]: {} },
      trails: { [groundTruthKey("218b6d")]: [] },
    });
    stores.markerRegistry.set(groundTruthKey("218b6d"), {});

    const keys = applyGroundTruthFixes(fixes, { "652600": trail(34.62, -82.4996) }, META, 2000);
    pruneGroundTruthFixes(stores, keys);

    expect(Object.keys(fixes)).toEqual([groundTruthKey("652600")]);
    expect(stores.smooth[groundTruthKey("218b6d")]).toBeUndefined();
    expect(stores.trails[groundTruthKey("218b6d")]).toBeUndefined();
    expect(stores.markerRegistry.has(groundTruthKey("218b6d"))).toBe(false);
    expect(stores.smooth[groundTruthKey("652600")]).toBeDefined();
  });

  it("leaves radar entries alone — they have their own staleness rule", () => {
    const fixes = { "652600": { hex: "652600", _key: "652600", position_source: "x" } };
    pruneGroundTruthFixes(makeStores(fixes), new Set());

    expect(fixes["652600"]).toBeDefined();
  });
});

describe("sweepStaleGroundTruthFixes", () => {
  it("drops truth objects whose feed has stopped, from every store", () => {
    const fixes: any = {};
    const now = 1_000_000;
    applyGroundTruthFixes(fixes, { abc: [[34.8, -82.4, 3000, 999]] }, {}, now - 40_000);
    const key = groundTruthKey("abc");
    const stores = makeStores(fixes, {
      smooth: { [key]: { lat: 34.8, lon: -82.4, track: 0 } },
      trails: { [key]: [[34.8, -82.4, 999]] },
    });
    sweepStaleGroundTruthFixes(stores, now, 30_000);
    expect(fixes[key]).toBeUndefined();
    expect(stores.smooth[key]).toBeUndefined();
    expect(stores.trails[key]).toBeUndefined();
  });

  it("keeps fresh truth and never touches radar entries", () => {
    const fixes: any = { r1: { hex: "r1", _updatedAt: 0 } };  // radar, ancient
    const now = 1_000_000;
    applyGroundTruthFixes(fixes, { abc: [[34.8, -82.4, 3000, 999]] }, {}, now - 1_000);
    sweepStaleGroundTruthFixes(makeStores(fixes), now, 30_000);
    expect(fixes[groundTruthKey("abc")]).toBeDefined();
    expect(fixes.r1).toBeDefined();  // radar staleness is someone else's rule
  });
});

describe("simulated debug parameters on fixes", () => {
  it("carries has_adsb / adsb_callsign / anomaly_event from meta", () => {
    const fixes: Record<string, any> = {};
    const meta = {
      "652600": {
        speed_ms: 148.8, heading: 217.3, object_type: "aircraft",
        has_adsb: true, adsb_callsign: "ABC1234", anomaly_event: "hijack",
        is_anomalous: true,
      },
    };
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, meta, 1000);
    const fix = fixes[groundTruthKey("652600")];
    expect(fix.has_adsb).toBe(true);
    expect(fix.adsb_callsign).toBe("ABC1234");
    expect(fix.anomaly_event).toBe("hijack");
    expect(fix.speed_ms).toBeCloseTo(148.8);
    expect(fix.heading).toBeCloseTo(217.3);
  });

  it("leaves the debug fields undefined when meta lacks them (old server)", () => {
    const fixes: Record<string, any> = {};
    applyGroundTruthFixes(fixes, { "652600": trail(34.61, -82.4996) }, META, 1000);
    const fix = fixes[groundTruthKey("652600")];
    expect(fix.has_adsb).toBeUndefined();
    expect(fix.adsb_callsign).toBeUndefined();
    expect(fix.anomaly_event).toBeUndefined();
  });
});
