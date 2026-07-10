import { useEffect, useRef } from "react";

/**
 * Two-way sync between a small bag of UI state and `location.hash`.
 *
 * Goals
 * -----
 *  * Shareable deep links — "give a teammate this URL and they land on
 *    the exact same view + selected aircraft."
 *  * Survives reload without trampling the user's preference cookies.
 *  * Lossy by design — only a handful of keys are encoded so the hash
 *    stays short and stable across feature additions.
 *
 * The hash format is `#k=v&k=v` (same syntax as a query string, sans `?`).
 * Unknown keys are ignored on read so old links keep working when fields
 * are removed.
 */

export interface HashState {
  /** Map centre lat (6dp) */
  lat?: number;
  /** Map centre lon (6dp) */
  lon?: number;
  /** Leaflet zoom */
  z?: number;
  /** Currently selected ICAO hex */
  hex?: string | null;
  /** Bitfield-ish toggles, encoded compactly */
  layers?: string;
}

function encodeLayers(t: Record<string, boolean>): string {
  // Each entry becomes a single letter when truthy.  Compact, stable,
  // and trivial to extend without breaking older links.
  let out = "";
  if (t.coverage)     out += "c";
  if (t.labels)       out += "l";
  if (t.trails)       out += "t";
  if (t.groundTruth)  out += "g";
  if (t.illuminators) out += "i";
  if (t.colorByAlt)   out += "a";
  if (t.stats)        out += "s";
  if (t.rangeRings)   out += "r";
  if (t.inBeamDiag)   out += "b";
  return out;
}

function decodeLayers(s: string | undefined): Partial<Record<string, boolean>> {
  if (!s) return {};
  return {
    coverage:     s.includes("c"),
    labels:       s.includes("l"),
    trails:       s.includes("t"),
    groundTruth:  s.includes("g"),
    illuminators: s.includes("i"),
    colorByAlt:   s.includes("a"),
    stats:        s.includes("s"),
    rangeRings:   s.includes("r"),
    inBeamDiag:   s.includes("b"),
  };
}

export function parseHash(): HashState {
  if (typeof window === "undefined") return {};
  const h = window.location.hash.replace(/^#/, "");
  if (!h) return {};
  const out: HashState = {};
  for (const part of h.split("&")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const k = part.slice(0, eq);
    const v = decodeURIComponent(part.slice(eq + 1));
    if (k === "lat" || k === "lon" || k === "z") {
      const n = Number(v);
      if (Number.isFinite(n)) out[k] = n;
    } else if (k === "hex") {
      out.hex = v || null;
    } else if (k === "layers") {
      out.layers = v;
    }
  }
  return out;
}

function writeHash(state: HashState) {
  if (typeof window === "undefined") return;
  const parts: string[] = [];
  if (state.lat != null && state.lon != null) {
    parts.push(`lat=${state.lat.toFixed(4)}`);
    parts.push(`lon=${state.lon.toFixed(4)}`);
  }
  if (state.z != null) parts.push(`z=${state.z}`);
  if (state.hex) parts.push(`hex=${encodeURIComponent(state.hex)}`);
  if (state.layers) parts.push(`layers=${state.layers}`);
  const next = parts.length ? `#${parts.join("&")}` : "";
  // Use replaceState so each pan/zoom doesn't fill the browser back stack.
  if (window.location.hash !== next) {
    const url = window.location.pathname + window.location.search + next;
    window.history.replaceState(null, "", url);
  }
}

/**
 * Throttled writer — call this from any UI event without worrying about
 * spamming the history API.
 */
export function useHashWriter() {
  const pendingRef = useRef<HashState | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, []);

  return (state: HashState) => {
    pendingRef.current = { ...(pendingRef.current ?? {}), ...state };
    if (timerRef.current) return;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      const next = pendingRef.current;
      pendingRef.current = null;
      if (next) writeHash(next);
    }, 250);
  };
}

export { encodeLayers, decodeLayers };
