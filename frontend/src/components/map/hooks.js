import { useEffect, useRef, useState, useCallback } from "react";
import { API_BASE, MAX_HISTORY } from "./constants";
import { mergeTrailPositions } from "./trails";

/**
 * Manages the WebSocket connection to /ws/aircraft with auto-reconnect,
 * plus an HTTP polling fallback when WS is unavailable.
 */
export function useAircraftFeed() {
  const [aircraft, setAircraft] = useState([]);
  const [connected, setConnected] = useState(false);

  const trailsRef = useRef({});
  const groundTruthRef = useRef({});
  const [trailTick, setTrailTick] = useState(0);

  const wsRef = useRef(null);
  const reconnectTimer = useRef(null);
  const pausedRef = useRef(false);
  const historyRef = useRef([]);

  const setPaused = useCallback((val) => {
    pausedRef.current = val;
  }, []);

  // Shared trail update logic used by both WS and HTTP polling
  const updateTrails = useCallback((newAircraft) => {
    const trails = trailsRef.current;
    const now = Date.now() / 1000;
    for (const ac of newAircraft) {
      if (!ac.lat || !ac.lon) continue;
      const hex = ac.hex;
      if (ac.recent_positions && ac.recent_positions.length > 0) {
        trails[hex] = mergeTrailPositions(trails[hex] || [], ac.recent_positions);
      } else {
        const existing = trails[hex] || [];
        const last = existing[existing.length - 1];
        if (
          !last ||
          Math.abs(last[0] - ac.lat) > 0.00005 ||
          Math.abs(last[1] - ac.lon) > 0.00005
        ) {
          trails[hex] = [...existing, [ac.lat, ac.lon, ac.alt_baro || 0, now]];
        }
      }
    }
    setTrailTick((t) => t + 1);
  }, []);

  // Shared history + state update
  const ingestAircraft = useCallback(
    (newAircraft, groundTruth) => {
      historyRef.current.push({ aircraft: newAircraft, ts: Date.now() });
      if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();

      if (!pausedRef.current) setAircraft(newAircraft);
      if (groundTruth && typeof groundTruth === "object") {
        groundTruthRef.current = groundTruth;
      }
      updateTrails(newAircraft);
    },
    [updateTrails],
  );

  // --- WebSocket connection with reconnect ---
  const connectWs = useCallback(() => {
    if (wsRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/aircraft`);

    ws.onopen = () => setConnected(true);

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        ingestAircraft(data.aircraft || [], data.ground_truth);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      reconnectTimer.current = setTimeout(connectWs, 3000);
    };

    ws.onerror = () => ws.close();
    wsRef.current = ws;
  }, [ingestAircraft]);

  useEffect(() => {
    connectWs();
    return () => {
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connectWs]);

  // --- HTTP polling fallback ---
  useEffect(() => {
    if (connected) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/radar/data/aircraft.json`);
        if (res.ok) {
          const data = await res.json();
          ingestAircraft(data.aircraft || [], data.ground_truth);
        }
      } catch {
        /* ignore */
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [connected, ingestAircraft]);

  return {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
    trailTick,
    historyRef,
    setPaused,
  };
}

/**
 * Fetch radar node positions for coverage zones.
 */
export function useNodes() {
  const [nodes, setNodes] = useState([]);

  useEffect(() => {
    async function loadNodes() {
      try {
        const res = await fetch(`${API_BASE}/radar/analytics`);
        if (!res.ok) return;
        const data = await res.json();
        const nodeList = [];
        for (const [id, info] of Object.entries(data.nodes || {})) {
          const da = info.detection_area;
          if (da) {
            nodeList.push({
              node_id: id,
              rx_lat: da.rx.lat,
              rx_lon: da.rx.lon,
              tx_lat: da.tx.lat,
              tx_lon: da.tx.lon,
              beam_azimuth_deg: da.beam_azimuth_deg,
              beam_width_deg: da.beam_width_deg,
              max_range_km: da.max_range_km,
            });
          }
        }
        setNodes(nodeList);
      } catch {
        /* ignore */
      }
    }
    loadNodes();
    const interval = setInterval(loadNodes, 30000);
    return () => clearInterval(interval);
  }, []);

  return nodes;
}
