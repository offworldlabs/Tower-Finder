import { useEffect, useRef, useState, useCallback } from "react";
import { API_BASE, ARC_TOTAL_LIFE_MS, MAX_HISTORY } from "./constants";
import { upsertArcEntries } from "./arcBuffer";
import { updateDetections } from "./detections";
import { mergeTrailPositions } from "./trails";
import { validLatLon } from "./geo";
import type { RadarNode } from "../../types";
import { usesRealOnlyFeed } from "../../utils/domains";
import { fetchMe, fetchMyNodes } from "../../api";

/**
 * Manages the WebSocket connection to /ws/aircraft with auto-reconnect,
 * plus an HTTP polling fallback when WS is unavailable.
 *
 * When `ownerOnly` is true, connects to /ws/aircraft/owner instead — a
 * server-filtered feed authenticated by the auth_token cookie that only emits
 * aircraft/arcs for nodes the logged-in user owns. The HTTP polling fallback
 * is disabled in this mode because the public aircraft.json is unfiltered and
 * would leak other nodes' data.
 */
export function useAircraftFeed(ownerOnly = false) {
  const [aircraft, setAircraft] = useState([]);
  const [connected, setConnected] = useState(false);

  const trailsRef = useRef({});
  const groundTruthRef = useRef({});
  const groundTruthMetaRef = useRef({});
  const anomalyHexesRef = useRef(new Set());
  const [trailTick, setTrailTick] = useState(0);
  // Separate tick that only increments when ground-truth data is replaced —
  // prevents the 8000-entry truthOnlyAircraft memo from re-running on every
  // trail update (which happens every WS message via updateTrails).
  const [groundTruthTick, setGroundTruthTick] = useState(0);

  const wsRef = useRef(null);
  const reconnectTimer = useRef(null);
  const reconnectAttempts = useRef(0);
  // True once the owning effect has cleaned up — stops the async onclose
  // handler from resurrecting the socket after unmount.
  const wsClosedRef = useRef(false);
  const pausedRef = useRef(false);
  const historyRef = useRef([]);
  // Watchdog: timestamp of last received WS message — detects zombie connections
  // where the server has dropped us but onclose never fires (dead TCP, no FIN)
  const lastMsgRef = useRef(Date.now());

  // Detection arc accumulation buffer: key → ArcBufferEntry (see arcBuffer.ts:
  // {hex, node_id, ambiguity_arc, delay_us, alt_baro, doppler_hz,
  // target_class, ts}).  Keyed by hex + node + measured delay (quantized to
  // 0.1 µs) so an unchanged measurement refreshes one entry's fade clock
  // rather than stacking parallel strokes.  Entries persist for
  // ARC_TOTAL_LIFE_MS after last refresh, enabling fade-out per detection.
  const arcsBufferRef = useRef({});

  // Detection-presence oracle: "hex|node_id" → ts of last time that node
  // contributed to a track for that aircraft.  Populated from EVERY detection
  // shape (single-node arc, single-node no-arc, and multinode via
  // contributing_node_ids), so it is a complete "is this aircraft currently
  // detected by this node" record — unlike the arc buffer, which only holds
  // arc-bearing single-node detections.  TTL-pruned on the same grace window
  // as the arc buffer (don't flag a detection that only just expired).
  const detectionsRef = useRef({});

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
      if (!validLatLon(ac.lat, ac.lon)) continue;
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
    (newAircraft, groundTruth, groundTruthMeta, anomalyHexes, detectingNodes) => {
      historyRef.current.push({ aircraft: newAircraft, ts: Date.now() });
      if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();

      if (!pausedRef.current) setAircraft(newAircraft);
      if (groundTruth && typeof groundTruth === "object") {
        groundTruthRef.current = groundTruth;
        setGroundTruthTick((t) => t + 1);
      }
      if (groundTruthMeta && typeof groundTruthMeta === "object") {
        groundTruthMetaRef.current = groundTruthMeta;
      }
      if (Array.isArray(anomalyHexes)) {
        anomalyHexesRef.current = new Set(anomalyHexes);
      }

      // Accumulate detection arcs as a radar-style afterglow trail.  Keyed
      // by hex + node + MEASURED delay (see arcBuffer.ts): re-ingesting an
      // unchanged measurement refreshes the one existing ellipse's fade
      // clock — a stationary aircraft stays visibly bright as a single
      // stroke instead of stacking five parallel offset strokes — while a
      // changed delay lays a new ellipse at new geometry, which is the
      // trail.  Also prunes entries older than ARC_MAX_AGE_MS (already
      // faded to zero opacity in the renderer) to keep the buffer bounded.
      const now = Date.now();
      const ARC_MAX_AGE_MS = ARC_TOTAL_LIFE_MS;
      upsertArcEntries(arcsBufferRef.current, newAircraft, now, ARC_MAX_AGE_MS);

      // Detection-presence oracle (consumed by InBeamDiagnostic and the
      // GT debug panel).  Unions per-aircraft signals with the top-level
      // detecting_nodes feed key — see detections.ts.
      updateDetections(
        detectionsRef.current, newAircraft, detectingNodes, now, ARC_MAX_AGE_MS,
      );

      updateTrails(newAircraft);
    },
    [updateTrails],
  );

  // --- WebSocket connection with reconnect ---
  const connectWs = useCallback(() => {
    if (wsRef.current || wsClosedRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    // Owner mode overrides the public feeds with a server-filtered, cookie-authed feed.
    // Otherwise map.retina.fm streams only the real radar node; testmap streams all.
    const wsPath = ownerOnly
      ? "/ws/aircraft/owner"
      : usesRealOnlyFeed ? "/ws/aircraft/live" : "/ws/aircraft";
    const ws = new WebSocket(`${proto}//${window.location.host}${wsPath}`);

    ws.onopen = () => {
      setConnected(true);
      reconnectAttempts.current = 0;  // reset backoff on successful connect
      lastMsgRef.current = Date.now(); // reset watchdog so we don't misfire on slow first message
    };

    ws.onmessage = (evt) => {
      lastMsgRef.current = Date.now(); // keep watchdog alive
      try {
        const data = JSON.parse(evt.data);
        ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes, data.detecting_nodes);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      // Unmounted: onclose fires *after* the effect cleanup ran, so without
      // this guard it re-scheduled connectWs and opened a fresh socket that
      // outlived the component (and called setConnected on an unmounted one).
      if (wsClosedRef.current) return;
      setConnected(false);
      wsRef.current = null;
      // Exponential backoff: 3s, 6s, 12s … capped at 30s
      const delay = Math.min(3000 * Math.pow(2, reconnectAttempts.current), 30000);
      reconnectAttempts.current += 1;
      reconnectTimer.current = setTimeout(connectWs, delay);
    };

    ws.onerror = () => ws.close();
    wsRef.current = ws;
  }, [ingestAircraft, ownerOnly]);

  useEffect(() => {
    wsClosedRef.current = false;
    connectWs();
    return () => {
      wsClosedRef.current = true;
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
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
    // No HTTP fallback in owner mode: the public aircraft.json is unfiltered,
    // so polling it would leak other nodes' data. Wait for the WS to reconnect.
    if (ownerOnly) return;
    // On map.retina.fm use the real-node-only endpoint so unfiltered synthetic
    // aircraft never appear even when the WS is temporarily disconnected.
    const pollPath = usesRealOnlyFeed
      ? `${API_BASE}/radar/data/aircraft-live.json`
      : `${API_BASE}/radar/data/aircraft.json`;
    const controller = new AbortController();
    const doFetch = async () => {
      try {
        const res = await fetch(pollPath, { signal: controller.signal });
        if (res.ok) {
          const data = await res.json();
          ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes, data.detecting_nodes);
        }
      } catch (err) {
        if (err.name !== "AbortError") {
          /* ignore transient network errors */
        }
      }
    };
    // Fire immediately so data appears before the first interval tick.
    // This cuts the blank-map startup window from ~1s to the HTTP round-trip.
    doFetch();
    const interval = setInterval(doFetch, 1000);
    return () => {
      clearInterval(interval);
      controller.abort();
    };
  }, [connected, ingestAircraft, ownerOnly]);

  return {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
    groundTruthMetaRef,
    anomalyHexesRef,
    trailTick,
    groundTruthTick,
    historyRef,
    setPaused,
    arcsBufferRef,
    detectionsRef,
  };
}

/**
 * Returns a deterministic [dLat, dLon] privacy offset for a node's RX display location.
 * Uses a simple djb2-derived hash of the node_id string so the same node always gets
 * the same offset (stable display), but the true operator location cannot be read from
 * the map. Max offset ≈ ±400 m (0.0036°).
 */
function nodeDisplayFuzz(nodeId) {
  // Murmur-style hash — two independent seeds for lat and lon.
  // Avoids collisions between sequential IDs like node_001 / node_002.
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < nodeId.length; i++) {
    const c = nodeId.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  // Avalanche finaliser
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507);
  h1 = Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  h1 ^= h1 >>> 16;
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507);
  h2 = Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 ^= h2 >>> 16;
  // Normalise to [-1, 1) and scale to ±0.0036° ≈ ±400 m
  const n1 = ((h1 >>> 0) / 0x100000000) * 2 - 1;
  const n2 = ((h2 >>> 0) / 0x100000000) * 2 - 1;
  return [n1 * 0.0036, n2 * 0.0036];
}

/**
 * Fetch radar node positions for coverage zones.
 */
export function useNodes() {
  const [nodes, setNodes] = useState<RadarNode[]>([]);

  useEffect(() => {
    // Cancelled on unmount: this poll had no guard at all, so an in-flight
    // response resolved into setNodes on an unmounted component.
    const controller = new AbortController();
    async function loadNodes() {
      try {
        // On map.retina.fm request only real nodes from the backend — avoids
        // relying on client-side hostname detection to filter 900+ synthetic markers.
        const url = usesRealOnlyFeed
          ? `${API_BASE}/radar/analytics?real_only=true`
          : `${API_BASE}/radar/analytics`;
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) return;
        const data = await res.json();
        if (controller.signal.aborted) return;
        const nodeList: RadarNode[] = [];
        for (const [id, info] of Object.entries(data.nodes || {})) {
          // Mirror backend's is_synthetic_node() prefix list. The backend
          // already strips these from real_only feeds, but the analytics
          // endpoint without real_only=true returns them. Also catch any
          // leftover e2e/test-leak via a defensive client filter.
          if (
            usesRealOnlyFeed && (
              id.startsWith("synth-") ||
              id.startsWith("e2e-") ||
              id.startsWith("test-") ||
              id.startsWith("realnode-")
            )
          ) continue;
          const da = (info as any).detection_area;
          const ec = (info as any).empirical_coverage;
          if (da) {
            // Skip null-island nodes (rx=(0,0)) that result from backend
            // register_node() defaulting missing rx/tx coords to 0.  These
            // show up after HTTP-registration without a config block
            // (notably e2e bulk tests) and render as a stray marker in the
            // Atlantic Ocean.  Use a small epsilon so we still allow a real
            // node legitimately near the equator/prime-meridian, but
            // dismiss the exact-zero default sentinel.
            const rxLat = da.rx.lat;
            const rxLon = da.rx.lon;
            if (Math.abs(rxLat) < 1e-6 && Math.abs(rxLon) < 1e-6) continue;
            // Deterministic privacy fuzz for RX location — same node_id always gets the
            // same offset so the map is stable, but the true operator location cannot be
            // read directly from the display. ±~400m radius (≈0.0036°).
            const [dLat, dLon] = nodeDisplayFuzz(id);
            nodeList.push({
              node_id: id,
              rx_lat: rxLat + dLat,
              rx_lon: rxLon + dLon,
              // Unfuzzed coords for client-side bistatic-arc rebuild — the
              // backend builds the arc from the true RX, so fuzzing the
              // rebuild inputs would offset the arc by ~400 m perpendicular
              // to the locus relative to the backend's curve.
              rx_lat_real: rxLat,
              rx_lon_real: rxLon,
              tx_lat: da.tx.lat,
              tx_lon: da.tx.lon,
              // Node altitudes (m ASL) for the altitude-corrected arc
              // rebuild.  The analytics detection_area payload currently
              // emits rx/tx as {lat, lon} only (no alt field), so these
              // resolve to null and buildBistaticArc falls back to
              // h_rx = h_tx = 0.  Read defensively so the values are picked
              // up automatically if the backend starts emitting them.
              rx_alt_m: da.rx.alt ?? null,
              tx_alt_m: da.tx.alt ?? null,
              beam_azimuth_deg: da.beam_azimuth_deg,
              beam_width_deg: da.beam_width_deg,
              max_range_km: da.max_range_km,
              // Null for a node that declares no differential limit, which
              // keeps the legacy circular sector.
              max_bistatic_range_km: da.max_bistatic_range_km ?? null,
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
    return () => {
      controller.abort();
      clearInterval(interval);
    };
  }, []);

  return nodes;
}

/**
 * Resolves the current user (via the shared auth_token cookie) and the set of
 * node ids they own. `user` is null when not authenticated. Used to gate the
 * node-owner view on the testmap.
 */
export function useAuth() {
  const [user, setUser] = useState(null);
  const [ownedNodeIds, setOwnedNodeIds] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const me = await fetchMe();
      if (cancelled) return;
      if (me && me.email) {
        setUser(me);
        const myNodes = await fetchMyNodes();
        if (!cancelled) setOwnedNodeIds((myNodes || []).map((n) => n.node_id));
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, []);

  return { user, ownedNodeIds, loading };
}
