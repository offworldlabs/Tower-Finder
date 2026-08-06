// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { ARC_HOLD_MS, ARC_FADE_MS, ARC_TOTAL_LIFE_MS, dopplerColor } from "./constants";
import { buildBistaticArc, computeBistaticDelayUs } from "./bistaticArc";

/* ── DetectionArcs: imperative Leaflet polylines with timer-driven opacity fade.

      Each arc in the buffer is rendered as its own polyline that fades on
      its own clock — producing a radar-style afterglow trail behind a moving
      target. Each polyline fades linearly to zero over ARC_FADE_MS.  Tick
      interval is 250 ms which is enough resolution for visibly smooth decay
      without React re-render cost. ── */
const DetectionArcs = memo(function DetectionArcs({ arcsBufferRef, selectedHex, onSelect, onSelectNode, smoothRef, nodesByIdRef }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // key → { line: L.polyline, ts, hex, node_id, ... }
  const onSelectRef = useRef(onSelect);
  const onSelectNodeRef = useRef(onSelectNode);
  const selectedHexRef = useRef(selectedHex);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);
  useEffect(() => { onSelectNodeRef.current = onSelectNode; }, [onSelectNode]);
  useEffect(() => { selectedHexRef.current = selectedHex; }, [selectedHex]);

  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      const buf = arcsBufferRef.current;
      const now = Date.now();
      // Lifecycle: ARC_HOLD_MS at full opacity (currently 0 — branch is
      // dormant), then linear fade over ARC_FADE_MS.  Constants live in
      // map/constants.ts so the buffer pruner in hooks.ts shares the same
      // TTL via ARC_TOTAL_LIFE_MS.
      const curSelected = selectedHexRef.current;

      const seen = new Set();

      // Add / update arcs from buffer
      for (const key of Object.keys(buf)) {
        const entry = buf[key];
        if (!Array.isArray(entry.ambiguity_arc) || entry.ambiguity_arc.length < 2) continue;
        const age = now - entry.ts;
        if (age > ARC_TOTAL_LIFE_MS) continue;

        seen.add(key);
        const isSelected = curSelected != null && entry.hex === curSelected;
        // Opacity depends only on the ellipse's own age — not on whether
        // its aircraft is currently selected.  Selection affects colour
        // and stroke weight (below), so the user can still tell which
        // trail belongs to the selection, but each ellipse fades on its
        // own independent clock.
        let opacity;
        if (age <= ARC_HOLD_MS) {
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
        const weight = isSelected ? 6 : 4;

        const existing = polyMap.get(key);
        if (existing) {
          // Refresh color/weight too: a track can flip target_class
          // (aircraft↔drone) or its doppler band mid-flight, and we want
          // the polyline to reflect the latest classification rather than
          // freezing the colour at first-paint until the arc expires.
          // Geometry is frozen at creation: a bistatic arc is the locus of
          // positions consistent with the delay measured at detection time, so
          // it stays put and fades in place as the aircraft flies on. Only the
          // visual style is refreshed (a track can flip target_class or doppler
          // band mid-flight, and the selected arc switches to amber).
          existing.line.setStyle({ opacity, color, weight });
        } else {
          // First creation: compute the effective bistatic delay from the
          // current dead-reckoned icon position (sm) so the arc passes
          // through the icon.  The rebuilt locus spans the node's whole
          // detection area (same semantics as the backend arc) — only the
          // delay is refreshed from the icon position.  Falls back to the
          // backend arc when we don't have node geometry yet (useNodes
          // still loading).
          let arcPoints = entry.ambiguity_arc;
          if (entry.hex && entry.node_id) {
            const node = nodesByIdRef?.current?.[entry.node_id];
            const sm = smoothRef?.current?.[entry.hex];
            if (node && sm) {
              const rxLat = node.rx_lat_real ?? node.rx_lat;
              const rxLon = node.rx_lon_real ?? node.rx_lon;
              const effectiveDelay = computeBistaticDelayUs(sm.lat, sm.lon, rxLat, rxLon, node.tx_lat, node.tx_lon);
              if (effectiveDelay > 0) {
                const rebuilt = buildBistaticArc(effectiveDelay, node);
                if (rebuilt && rebuilt.length >= 2) arcPoints = rebuilt;
              }
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
      }

      // Remove arcs no longer in seen — either past their fade window or
      // their buffer entry was pruned.
      for (const [key, info] of polyMap) {
        if (seen.has(key)) continue;
        info.line.remove();
        polyMap.delete(key);
      }
    };

    // Initial tick + interval for smooth updates
    tick();
    const intervalId = setInterval(tick, 250);
    return () => {
      clearInterval(intervalId);
      for (const info of polyMap.values()) info.line.remove();
      polyMap.clear();
    };
  }, [map, arcsBufferRef, smoothRef, nodesByIdRef]);

  return null;
});

export default DetectionArcs;
