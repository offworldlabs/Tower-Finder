// Detection-arc afterglow buffer — pure keying / upsert logic.
//
// Extracted from useAircraftFeed (hooks.ts) so the keying decision is
// testable without mounting the hook.  The buffer maps key → entry; the
// renderer (DetectionArcs) draws one polyline per entry, each fading on its
// own clock (entry.ts).
//
// Keying: `${hex}-${node_id}-d<delay quantized to 0.1 µs>`.  Keying by the
// MEASURED delay (not the ingest timestamp) means a re-ingest of an unchanged
// measurement refreshes ONE polyline's fade clock instead of stacking a new
// offset stroke — staging measured 180/190 consecutive ticks where delay_us
// was unchanged while the old timestamp keying laid 5 concurrent parallel
// strokes per (hex, node).  A changed delay maps to a new key and lays a new
// entry: that is the genuine afterglow trail.
//
// Entries without a usable delay_us fall back to timestamp keying
// (`…-t<ingest ms>`) — the old behavior — because there is no measurement
// value to dedupe on; these keep the original same-millisecond-collision
// guard (skip rather than overwrite, so an existing entry keeps fading from
// its original ts).

/** Quantization step for the delay portion of the buffer key (µs). */
export const ARC_DELAY_KEY_QUANT_US = 0.1;

export interface ArcBufferEntry {
  hex: string;
  node_id: string;
  /** Backend-built 2D locus — fallback when the client can't rebuild. */
  ambiguity_arc: [number, number][];
  /**
   * Measured bistatic delay (µs) — the only bistatic parameter needed to
   * rebuild the locus client-side; geometry comes from useNodes.
   */
  delay_us: number | null;
  /** Barometric altitude (feet) at ingest — 3D altitude correction input. */
  alt_baro: number | null;
  doppler_hz: number;
  target_class?: string;
  /** Fade-clock anchor: refreshed when the same measurement is re-ingested. */
  ts: number;
}

/**
 * Buffer key for one detection: hex + node + measured delay (quantized to
 * ARC_DELAY_KEY_QUANT_US), falling back to the ingest timestamp when the
 * entry carries no usable delay_us.
 */
export function arcBufferKey(
  hex: string,
  nodeId: string,
  delayUs: number | null | undefined,
  nowMs: number,
): string {
  if (delayUs != null && Number.isFinite(delayUs)) {
    return `${hex}-${nodeId}-d${Math.round(delayUs / ARC_DELAY_KEY_QUANT_US)}`;
  }
  return `${hex}-${nodeId}-t${nowMs}`;
}

/**
 * Ingest one batch of aircraft into the arc buffer (mutates `buf`).
 *
 * - New (hex, node, delay) → new entry, fading from `nowMs`.
 * - Re-ingest of an existing (hex, node, delay) → refresh that entry's fade
 *   clock (ts = nowMs) and its style-relevant fields (doppler, class,
 *   altitude).  Geometry stays frozen: same delay ⇒ same locus, and the
 *   renderer freezes polyline geometry per key anyway.
 * - Timestamp-keyed entries (no delay_us): a key collision means two ingests
 *   in the same millisecond — skip rather than overwrite so the existing
 *   entry keeps fading from its original ts (original guard semantics).
 * - Entries older than `maxAgeMs` are pruned (they have already faded to
 *   zero opacity in the renderer); keeps the buffer bounded.
 */
export function upsertArcEntries(
  buf: Record<string, ArcBufferEntry>,
  aircraft: any[],
  nowMs: number,
  maxAgeMs: number,
): void {
  for (const ac of aircraft) {
    if (!Array.isArray(ac.ambiguity_arc) || ac.ambiguity_arc.length < 2 || !ac.node_id) {
      continue;
    }
    const delayUs =
      ac.delay_us != null && Number.isFinite(ac.delay_us) ? ac.delay_us : null;
    const key = arcBufferKey(ac.hex, ac.node_id, delayUs, nowMs);
    const existing = buf[key];
    if (existing) {
      // Timestamp-keyed collision (same-millisecond double ingest): keep the
      // original entry fading from its original ts.
      if (delayUs == null) continue;
      // Delay-keyed re-ingest of the same measurement: refresh the fade
      // clock and style fields of the ONE existing polyline.
      existing.ts = nowMs;
      existing.doppler_hz = ac.doppler_hz ?? 0;
      existing.target_class = ac.target_class;
      if (ac.alt_baro != null && Number.isFinite(ac.alt_baro)) {
        existing.alt_baro = ac.alt_baro;
      }
      continue;
    }
    buf[key] = {
      hex: ac.hex,
      node_id: ac.node_id,
      ambiguity_arc: ac.ambiguity_arc,
      delay_us: delayUs,
      alt_baro:
        ac.alt_baro != null && Number.isFinite(ac.alt_baro) ? ac.alt_baro : null,
      doppler_hz: ac.doppler_hz ?? 0,
      target_class: ac.target_class,
      ts: nowMs,
    };
  }
  for (const key of Object.keys(buf)) {
    if (nowMs - buf[key].ts > maxAgeMs) delete buf[key];
  }
}
