import { useEffect, useRef } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import { buildViewportSnapshot, getFocusPoints } from "./geo";

export function FitBounds({ aircraft, nodes, selectedHex, focusNonce }) {
  const map = useMap();
  const initialFitted = useRef(false);
  const userMoved = useRef(false);
  const lastFocusNonce = useRef(null);

  useEffect(() => {
    const onMove = () => {
      userMoved.current = true;
    };
    map.on("dragstart", onMove);
    map.on("zoomstart", onMove);
    return () => {
      map.off("dragstart", onMove);
      map.off("zoomstart", onMove);
    };
  }, [map]);

  useEffect(() => {
    const isExplicit = focusNonce !== lastFocusNonce.current;
    if (initialFitted.current && userMoved.current && !isExplicit) return;

    const pts = getFocusPoints(aircraft, nodes, selectedHex);

    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [60, 60], animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    } else if (pts.length === 1) {
      map.setView(pts[0], 10, { animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    }
  }, [aircraft, nodes, selectedHex, focusNonce, map]);

  return null;
}

export function ViewportTracker({ onChange }) {
  const map = useMapEvents({
    moveend: () => onChange(buildViewportSnapshot(map.getBounds())),
    zoomend: () => onChange(buildViewportSnapshot(map.getBounds())),
    resize: () => onChange(buildViewportSnapshot(map.getBounds())),
  });

  useEffect(() => {
    onChange(buildViewportSnapshot(map.getBounds()));
  }, [map, onChange]);

  return null;
}
