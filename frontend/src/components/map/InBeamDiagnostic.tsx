// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { isInBeam } from "./geo";

/* ── InBeamDiagnostic: flags ADS-B aircraft inside a node's beam that
      have no recent confirmed detection from that node.  Renders a
      dashed red polyline from the node's RX position to the aircraft —
      one per (aircraft, node) pair — so the "missing link" is
      geometrically visible.  Reads detectionsRef as the recently-detected
      oracle — a "hex|node_id" → ts map covering every detection shape
      (single-node and multinode), TTL-pruned so its grace period matches
      the spec (don't flag a detection that only just expired).  Thresholds
      are tightened to 0.9 × beam width and 0.95 × max range to avoid
      flagging aircraft that are only momentarily clipping the edges. ── */

const BEAM_WIDTH_FACTOR = 0.9;
const MAX_RANGE_FACTOR = 0.95;

const InBeamDiagnostic = memo(function InBeamDiagnostic({ detectionsRef, groundTruthRef, nodesByIdRef, smoothRef }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // pairKey → L.polyline

  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      // "(hex, node_id) recently detected" map, maintained in the aircraft
      // feed. A present key means that node contributed a detection for that
      // aircraft within the grace window (ARC_TOTAL_LIFE_MS).
      const recentDetections = detectionsRef.current || {};

      const truth = groundTruthRef.current || {};
      const nodes = nodesByIdRef?.current || {};
      const seen = new Set();

      const smooth = smoothRef?.current || {};

      for (const [hex, trail] of Object.entries(truth)) {
        if (!Array.isArray(trail) || trail.length === 0) continue;
        const last = trail[trail.length - 1];
        if (!last) continue;
        // Beam membership is tested against the raw ground-truth sample —
        // the freshest real fix. A dead-reckoned position extrapolates the
        // last velocity and, for a stalled/lost track, can glide across a
        // beam edge and fabricate (or hide) a gap, so it must NOT drive the
        // in/out decision.
        const beamLat = last[0];
        const beamLon = last[1];
        if (beamLat == null || beamLon == null) continue;
        // The drawn endpoint, by contrast, prefers the dead-reckoned position
        // (same source that draws the aircraft dot) so the line's far end
        // lands on the icon rather than lagging behind it by one update.
        const s = smooth[hex];
        const acLat = s ? s.lat : beamLat;
        const acLon = s ? s.lon : beamLon;

        for (const [nodeId, node] of Object.entries(nodes)) {
          if (recentDetections[`${hex}|${nodeId}`] != null) continue;

          const { rx_lat: rxLat, rx_lon: rxLon, beam_azimuth_deg: azimuth, beam_width_deg: beamWidth, max_range_km: maxRange } = node;
          if (rxLat == null || rxLon == null || azimuth == null || beamWidth == null || maxRange == null) continue;

          if (!isInBeam(rxLat, rxLon, azimuth, beamWidth * BEAM_WIDTH_FACTOR, maxRange * MAX_RANGE_FACTOR, beamLat, beamLon)) continue;

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
  }, [map, detectionsRef, groundTruthRef, nodesByIdRef, smoothRef]);

  return null;
});

export default InBeamDiagnostic;
