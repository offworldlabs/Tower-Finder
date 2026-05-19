import { useEffect, useRef } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import { buildViewportSnapshot, getFocusPoints } from "./geo";

export function FitBounds({ aircraft, nodes, selectedHex, focusNonce }) {
  const map = useMap();
  const initialFitted = useRef(false);
  const userMoved = useRef(false);
  const lastFocusNonce = useRef(null);
  const prevSelectedHex = useRef(selectedHex);

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
    // Deselecting an aircraft (selectedHex N→null) without an explicit focus
    // bump must NOT refit: the user clicked the map to dismiss the selection,
    // not to reset the viewport. Without this guard the effect re-runs with
    // userMoved=false (click doesn't fire dragstart/zoomstart) and refits to
    // all aircraft, yanking the user out of their pan/zoom.
    const wasSelected = prevSelectedHex.current;
    prevSelectedHex.current = selectedHex;
    if (initialFitted.current && !isExplicit && wasSelected && !selectedHex) {
      // Treat deselection as user intent — pin viewport in place.
      userMoved.current = true;
      return;
    }
    if (initialFitted.current && userMoved.current && !isExplicit) return;

    const pts = getFocusPoints(aircraft, nodes, selectedHex);

    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [60, 60], animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    } else if (pts.length === 1) {
      map.setView(pts[0], map.getZoom(), { animate: true, duration: 0.5 });
      initialFitted.current = true;
      lastFocusNonce.current = focusNonce;
      if (isExplicit) userMoved.current = false;
    }
  }, [aircraft, nodes, selectedHex, focusNonce, map]);

  return null;
}

export function MapClickClear({ onClear }) {
  useMapEvents({
    click: () => onClear(),
  });
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
