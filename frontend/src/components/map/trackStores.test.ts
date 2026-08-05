import { describe, it, expect } from "vitest";
import { forgetTrack, snapTrack, sweepStaleRadar } from "./trackStores";

function stores() {
  return {
    fixes: {}, smooth: {}, svgElems: {}, svgMiss: {}, latLng: {},
    trails: {}, lastTrailSample: {},
    markerRegistry: new Map(),
  };
}

describe("forgetTrack", () => {
  it("removes the key from every store, including the marker registry", () => {
    const s: any = stores();
    for (const k of ["fixes", "smooth", "svgElems", "svgMiss", "latLng", "trails", "lastTrailSample"]) {
      s[k]["abc"] = { any: 1 };
      s[k]["keep"] = { any: 2 };
    }
    s.markerRegistry.set("abc", {});
    forgetTrack("abc", s);
    for (const k of ["fixes", "smooth", "svgElems", "svgMiss", "latLng", "trails", "lastTrailSample"]) {
      expect(s[k]["abc"]).toBeUndefined();
      expect(s[k]["keep"]).toBeDefined();
    }
    expect(s.markerRegistry.has("abc")).toBe(false);
  });
});

describe("snapTrack", () => {
  it("mutates smooth and latLng in place and drops the trail buffers", () => {
    const s: any = stores();
    const sm = { lat: 1, lon: 2, track: 90 };
    const ll = { lat: 1, lng: 2 };
    s.smooth.abc = sm;
    s.latLng.abc = ll;
    s.trails.abc = [[1, 2, 3]];
    s.lastTrailSample.abc = 123;
    snapTrack("abc", s, 34.8, -82.4, 180);
    expect(s.smooth.abc).toBe(sm);        // same object — the loop holds it
    expect(sm.lat).toBe(34.8);
    expect(ll.lat).toBe(34.8);
    expect(ll.lng).toBe(-82.4);
    expect(s.trails.abc).toBeUndefined();
    expect(s.lastTrailSample.abc).toBeUndefined();
  });

  it("creates the smooth entry when absent", () => {
    const s: any = stores();
    snapTrack("abc", s, 34.8, -82.4, 45);
    expect(s.smooth.abc).toEqual({ lat: 34.8, lon: -82.4, track: 45 });
  });
});

describe("sweepStaleRadar", () => {
  it("forgets stale radar keys but never truth keys", () => {
    const s: any = stores();
    s.fixes.old = { _updatedAt: 0 };
    s.fixes.fresh = { _updatedAt: 999_000 };
    s.fixes["gt:x"] = { _updatedAt: 0, _isTruth: true };
    s.smooth.old = {};
    sweepStaleRadar(s, 1_000_000, 8000);
    expect(s.fixes.old).toBeUndefined();
    expect(s.smooth.old).toBeUndefined();
    expect(s.fixes.fresh).toBeDefined();
    expect(s.fixes["gt:x"]).toBeDefined();
  });
});
