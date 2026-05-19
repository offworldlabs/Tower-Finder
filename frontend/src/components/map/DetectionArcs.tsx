// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { ARC_HOLD_MS, ARC_FADE_MS, ARC_TOTAL_LIFE_MS, dopplerColor } from "./constants";
import { buildBistaticArc } from "./bistaticArc";

/* ── DetectionArcs: imperative Leaflet polylines with timer-driven opacity fade.

      Each arc in the buffer is rendered as its own polyline that fades on
      its own clock — producing a radar-style afterglow trail behind a moving
      target. Newer buckets sit at full brightness briefly, then linear-fade
      to zero over ~10 s. Tick interval is 250 ms which is enough resolution
      for visibly smooth decay without React re-render cost. ── */
const DetectionArcs = memo(function DetectionArcs({ arcsBufferRef, selectedHex, onSelect, onSelectNode, smoothRef, nodesByIdRef }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // key → { line: L.polyline, ts, hex, node_id, ... }
  // Pending arcs have hex=null and no aircraft entry, so no plane icon is rendered for them.
  // Without a marker the user reads "arcs without aircraft" as "false detection".
  // Each pending arc gets a small hollow circle at its midpoint, matching the arc's fade.
  const placeholderMapRef = useRef(new Map()); // key → L.circleMarker
  const onSelectRef = useRef(onSelect);
  const onSelectNodeRef = useRef(onSelectNode);
  const selectedHexRef = useRef(selectedHex);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);
  useEffect(() => { onSelectNodeRef.current = onSelectNode; }, [onSelectNode]);
  useEffect(() => { selectedHexRef.current = selectedHex; }, [selectedHex]);

  useEffect(() => {
    const polyMap = polyMapRef.current;
    const placeholderMap = placeholderMapRef.current;

    const tick = () => {
      const buf = arcsBufferRef.current;
      const now = Date.now();
      // Unified lifecycle for both bucketed (per-second hex-node-ts) and
      // pending (det-node-lat-lon) arcs: ARC_HOLD_MS at full opacity, then
      // linear fade over ARC_FADE_MS.  Constants live in map/constants.ts
      // so the buffer pruner in hooks.ts can share the same TTL.
      const curSelected = selectedHexRef.current;

      const seen = new Set();
      // Separate seen set for placeholder rings: a key being in `seen` (polyline
      // still in fade window) doesn't mean we still want a placeholder — at low
      // zoom we suppress the ring while keeping the polyline.
      const seenPlaceholders = new Set();

      // Add / update arcs from buffer
      for (const key of Object.keys(buf)) {
        const entry = buf[key];
        if (!Array.isArray(entry.ambiguity_arc) || entry.ambiguity_arc.length < 2) continue;
        // isPending controls weight (thinner stroke than bucketed arcs) and
        // whether a hollow-ring placeholder is drawn at the midpoint.
        const isPending = key.startsWith("det-");
        const age = now - entry.ts;
        if (age > ARC_TOTAL_LIFE_MS) continue;

        seen.add(key);
        // Pending arcs have hex=null.  Without the explicit null check this
        // would compare null === null when no aircraft is selected and treat
        // every pending arc as "selected".
        const isSelected = curSelected != null && entry.hex === curSelected;
        let opacity;
        if (isSelected) {
          opacity = 1.0;
        } else if (age <= ARC_HOLD_MS) {
          opacity = 1.0;
        } else {
          opacity = Math.max(0.0, 1.0 - (age - ARC_HOLD_MS) / ARC_FADE_MS);
        }
        // Selected aircraft's arc switches to amber so it stands out from
        // neighbouring doppler-coloured arcs at the same map region — useful
        // in dense areas where cyan/red arcs overlap and the +2 weight bump
        // alone isn't enough to identify which arc belongs to the selected
        // target.
        const color = isSelected
          ? "#fbbf24"
          : (entry.target_class === "drone" ? "#fb923c" : dopplerColor(entry.doppler_hz ?? 0));
        // Pending (unidentified) detections render thinner so they're visually
        // distinct from confirmed aircraft arcs.
        const weight = isSelected ? 6 : (isPending ? 2 : 4);

        const existing = polyMap.get(key);
        if (existing) {
          // Refresh color/weight too: a track can flip target_class
          // (aircraft↔drone) or its doppler band mid-flight, and we want
          // the polyline to reflect the latest classification rather than
          // freezing the colour at first-paint until the arc expires.
          existing.line.setStyle({ opacity, color, weight });
        } else {
          // Client-side bistatic-arc rebuild: when we know delay_us + the
          // source node's geometry + where the icon is right now, recompute
          // the trim segment along the same bistatic locus around the
          // dead-reckoned position.  This is the right way to make the arc
          // follow a moving aircraft: the curve stays physically valid (it's
          // a real point on the locus for the measured delay), only the
          // 25-km display window slides.  Earlier attempts that *translated*
          // the arc by (icon - midpoint) drew the curve in regions outside
          // any node's beam — wrong physics.  Falls back to the backend arc
          // when we don't have the geometry yet (e.g. node config still
          // loading) or for pending arcs (entry.hex is null).
          let arcPoints = entry.ambiguity_arc;
          if (entry.hex && entry.delay_us && entry.node_id) {
            const node = nodesByIdRef?.current?.[entry.node_id];
            const sm = smoothRef?.current?.[entry.hex];
            if (node && sm) {
              const rebuilt = buildBistaticArc(entry.delay_us, node, sm.lat, sm.lon);
              if (rebuilt && rebuilt.length >= 2) arcPoints = rebuilt;
            }
          }
          const line = L.polyline(arcPoints, {
            color,
            weight,
            opacity,
            lineCap: "round",
            lineJoin: "round",
          });
          line.on("click", (e) => {
            L.DomEvent.stopPropagation(e);
            if (entry.hex) onSelectRef.current?.(entry.hex, false);
            if (entry.node_id) onSelectNodeRef.current?.(entry.node_id);
          });
          line.addTo(map);
          polyMap.set(key, { line, ts: entry.ts, hex: entry.hex, node_id: entry.node_id });
        }

        // Pending-arc placeholder marker (hollow ring at midpoint).
        // Communicates "detection here, no aircraft identity yet" so the user
        // doesn't read pending arcs as "false positive arcs without aircraft".
        // Skipped at very low zoom because the arc itself shrinks to ~10 px
        // there and the screen-space ring would dominate the geometry.
        if (isPending) {
          if (map.getZoom() >= 7) {
            seenPlaceholders.add(key);
            const arc = entry.ambiguity_arc;
            const mid = arc[Math.floor(arc.length / 2)];
            const existingMarker = placeholderMap.get(key);
            if (existingMarker) {
              existingMarker.setLatLng(mid);
              existingMarker.setStyle({ opacity, color });
            } else {
              const marker = L.circleMarker(mid, {
                radius: 4,
                color,
                weight: 1.5,
                fillOpacity: 0,
                opacity,
                interactive: true,
              });
              marker.on("click", (e) => {
                L.DomEvent.stopPropagation(e);
                if (entry.node_id) onSelectNodeRef.current?.(entry.node_id);
              });
              marker.addTo(map);
              placeholderMap.set(key, marker);
            }
          }
          // else: zoom < 7 — leave any existing marker for the cleanup loop
          // below to remove (it's not added to seenPlaceholders, so it'll go).
        }
      }

      // Remove arcs no longer in seen — either past their fade window or
      // their buffer entry was pruned.
      for (const [key, info] of polyMap) {
        if (seen.has(key)) continue;
        info.line.remove();
        polyMap.delete(key);
      }
      for (const [key, marker] of placeholderMap) {
        if (seenPlaceholders.has(key)) continue;
        marker.remove();
        placeholderMap.delete(key);
      }
    };

    // Initial tick + interval for smooth updates
    tick();
    const intervalId = setInterval(tick, 250);
    // Zoom changes affect placeholder visibility (suppressed under zoom 7) —
    // re-tick immediately on zoomend so rings appear/disappear without a 250ms
    // lag.
    map.on("zoomend", tick);
    return () => {
      clearInterval(intervalId);
      map.off("zoomend", tick);
      for (const info of polyMap.values()) info.line.remove();
      polyMap.clear();
      for (const marker of placeholderMap.values()) marker.remove();
      placeholderMap.clear();
    };
  }, [map, arcsBufferRef, smoothRef, nodesByIdRef]);

  return null;
});

export default DetectionArcs;
