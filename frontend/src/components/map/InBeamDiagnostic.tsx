// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { isInBeam } from "./geo";

/* ── InBeamDiagnostic: flags ADS-B aircraft inside a node's beam that
      have no recent confirmed detection from that node.  Renders a
      dashed red polyline from the node's RX position to the aircraft —
      one per (aircraft, node) pair — so the "missing link" is
      geometrically visible.  Uses arcsBufferRef as the recently-detected
      oracle: its existing TTL gives the grace period the spec asks for
      (don't flag a detection that only just expired).  Thresholds are
      tightened to 0.9 × beam width and 0.95 × max range to avoid
      flagging aircraft that are only momentarily clipping the edges. ── */

const BEAM_WIDTH_FACTOR = 0.9;
const MAX_RANGE_FACTOR = 0.95;

const InBeamDiagnostic = memo(function InBeamDiagnostic({ arcsBufferRef, groundTruthRef, nodesByIdRef }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // pairKey → L.polyline

  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      // Build "(hex, node_id) currently detected" set from the arc buffer.
      // Any entry still in the buffer means there's been a confirmed
      // detection from that node for that aircraft within ARC_TOTAL_LIFE_MS.
      const recentDetections = new Set();
      const buf = arcsBufferRef.current || {};
      for (const key of Object.keys(buf)) {
        const e = buf[key];
        if (e?.hex && e?.node_id) recentDetections.add(`${e.hex}|${e.node_id}`);
      }

      const truth = groundTruthRef.current || {};
      const nodes = nodesByIdRef?.current || {};
      const seen = new Set();

      for (const [hex, trail] of Object.entries(truth)) {
        if (!Array.isArray(trail) || trail.length === 0) continue;
        const last = trail[trail.length - 1];
        if (!last) continue;
        const acLat = last[0];
        const acLon = last[1];
        if (acLat == null || acLon == null) continue;

        for (const [nodeId, node] of Object.entries(nodes)) {
          if (recentDetections.has(`${hex}|${nodeId}`)) continue;

          const { rx_lat: rxLat, rx_lon: rxLon, beam_azimuth_deg: azimuth, beam_width_deg: beamWidth, max_range_km: maxRange } = node;
          if (rxLat == null || rxLon == null || azimuth == null || beamWidth == null || maxRange == null) continue;

          if (!isInBeam(rxLat, rxLon, azimuth, beamWidth * BEAM_WIDTH_FACTOR, maxRange * MAX_RANGE_FACTOR, acLat, acLon)) continue;

          const pairKey = `${hex}|${nodeId}`;
          seen.add(pairKey);

          const latLngs = [[rxLat, rxLon], [acLat, acLon]];
          const existing = polyMap.get(pairKey);
          if (existing) {
            existing.setLatLngs(latLngs);
          } else {
            const line = L.polyline(latLngs, {
              color: "#ef4444", // red-500
              weight: 1.5,
              opacity: 0.7,
              dashArray: "4 6",
              interactive: false,
            });
            line.addTo(map);
            polyMap.set(pairKey, line);
          }
        }
      }

      // Drop polylines whose pair is no longer flagged — either the node
      // started detecting the aircraft, or the aircraft left the beam.
      for (const [pairKey, line] of polyMap) {
        if (seen.has(pairKey)) continue;
        line.remove();
        polyMap.delete(pairKey);
      }
    };

    tick();
    const intervalId = setInterval(tick, 500);
    return () => {
      clearInterval(intervalId);
      for (const line of polyMap.values()) line.remove();
      polyMap.clear();
    };
  }, [map, arcsBufferRef, groundTruthRef, nodesByIdRef]);

  return null;
});

export default InBeamDiagnostic;
