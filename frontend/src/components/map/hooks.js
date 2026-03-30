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
  const groundTruthMetaRef = useRef({});
  const anomalyHexesRef = useRef(new Set());
  const [trailTick, setTrailTick] = useState(0);

  const wsRef = useRef(null);
  const reconnectTimer = useRef(null);
  const reconnectAttempts = useRef(0);
  const pausedRef = useRef(false);
  const historyRef = useRef([]);
  // Watchdog: timestamp of last received WS message — detects zombie connections
  // where the server has dropped us but onclose never fires (dead TCP, no FIN)
  const lastMsgRef = useRef(Date.now());

  const setPaused = useCallback((val) => {
    pausedRef.current = val;
  }, []);

  // Prune trails for aircraft gone > 5 minutes — keeps memory bounded over long sessions
  const trailPruneRef = useRef(0);

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
    // Prune stale trail entries every 60 updates (~60s) to prevent unbounded growth
    trailPruneRef.current += 1;
    if (trailPruneRef.current >= 60) {
      trailPruneRef.current = 0;
      const activeHexes = new Set(newAircraft.map((ac) => ac.hex));
      const cutoff = now - 300; // 5 minutes
      for (const hex of Object.keys(trails)) {
        if (activeHexes.has(hex)) continue;
        const trail = trails[hex];
        const lastTs = trail?.[trail.length - 1]?.[3] ?? 0;
        if (lastTs < cutoff) delete trails[hex];
      }
    }
    setTrailTick((t) => t + 1);
  }, []);

  // Shared history + state update
  const ingestAircraft = useCallback(
    (newAircraft, groundTruth, groundTruthMeta, anomalyHexes) => {
      historyRef.current.push({ aircraft: newAircraft, ts: Date.now() });
      if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();

      if (!pausedRef.current) setAircraft(newAircraft);
      if (groundTruth && typeof groundTruth === "object") {
        groundTruthRef.current = groundTruth;
      }
      if (groundTruthMeta && typeof groundTruthMeta === "object") {
        groundTruthMetaRef.current = groundTruthMeta;
      }
      if (Array.isArray(anomalyHexes)) {
        anomalyHexesRef.current = new Set(anomalyHexes);
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

    ws.onopen = () => {
      setConnected(true);
      reconnectAttempts.current = 0;  // reset backoff on successful connect
      lastMsgRef.current = Date.now(); // reset watchdog so we don't misfire on slow first message
    };

    ws.onmessage = (evt) => {
      lastMsgRef.current = Date.now(); // keep watchdog alive
      try {
        const data = JSON.parse(evt.data);
        ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      // Exponential backoff: 3s, 6s, 12s … capped at 30s
      const delay = Math.min(3000 * Math.pow(2, reconnectAttempts.current), 30000);
      reconnectAttempts.current += 1;
      reconnectTimer.current = setTimeout(connectWs, delay);
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

  // --- Zombie-connection watchdog ---
  // Server sends aircraft data every ~2s. If we've had no message for 12s while
  // the WS appears OPEN, the connection is a zombie (server dropped us, TCP
  // still "open" with no FIN — onclose never fires). Force-close to trigger
  // the reconnect path and restart HTTP polling fallback.
  useEffect(() => {
    const WATCHDOG_MS = 12_000;
    const id = setInterval(() => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        if (Date.now() - lastMsgRef.current > WATCHDOG_MS) {
          ws.close(); // triggers onclose → reconnect + HTTP fallback
        }
      }
    }, 5_000);
    return () => clearInterval(id);
  }, []);

  // --- HTTP polling fallback ---
  useEffect(() => {
    if (connected) return;
    const controller = new AbortController();
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/radar/data/aircraft.json`, {
          signal: controller.signal,
        });
        if (res.ok) {
          const data = await res.json();
          ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes);
        }
      } catch (err) {
        if (err.name !== "AbortError") {
          /* ignore transient network errors */
        }
      }
    }, 1000);
    return () => {
      clearInterval(interval);
      controller.abort();
    };
  }, [connected, ingestAircraft]);

  return {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
    groundTruthMetaRef,
    anomalyHexesRef,
    trailTick,
    historyRef,
    setPaused,
  };
}

/**
 * Returns a deterministic [dLat, dLon] privacy offset for a node's RX display location.
 * Uses a simple djb2-derived hash of the node_id string so the same node always gets
 * the same offset (stable display), but the true operator location cannot be read from
 * the map. Max offset ≈ ±400 m (0.0036°).
 */
function nodeDisplayFuzz(nodeId) {
  // Two independent hash passes — one for lat, one for lon
  let h1 = 5381, h2 = 52711;
  for (let i = 0; i < nodeId.length; i++) {
    const c = nodeId.charCodeAt(i);
    h1 = ((h1 << 5) + h1) ^ c;
    h2 = ((h2 << 5) + h2) ^ (c * 1000003);
  }
  // Normalise to [0, 1) and map to [-1, 1)
  const n1 = ((h1 >>> 0) / 0xFFFFFFFF) * 2 - 1;
  const n2 = ((h2 >>> 0) / 0xFFFFFFFF) * 2 - 1;
  // 0.0036° ≈ 400 m at mid latitudes
  return [n1 * 0.0036, n2 * 0.0036];
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
          const ec = info.empirical_coverage;
          if (da) {
            // Deterministic privacy fuzz for RX location — same node_id always gets the
            // same offset so the map is stable, but the true operator location cannot be
            // read directly from the display. ±~400m radius (≈0.0036°).
            const [dLat, dLon] = nodeDisplayFuzz(id);
            nodeList.push({
              node_id: id,
              rx_lat: da.rx.lat + dLat,
              rx_lon: da.rx.lon + dLon,
              tx_lat: da.tx.lat,
              tx_lon: da.tx.lon,
              beam_azimuth_deg: da.beam_azimuth_deg,
              beam_width_deg: da.beam_width_deg,
              max_range_km: da.max_range_km,
              empirical_polygon: ec?.polygon ?? null,
              empirical_n_points: ec?.n_points ?? 0,
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
