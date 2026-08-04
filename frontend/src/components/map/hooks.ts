import { useEffect, useRef, useState, useCallback } from "react";
import { API_BASE, ARC_TOTAL_LIFE_MS, MAX_HISTORY } from "./constants";
import { mergeTrailPositions } from "./trails";
import { validLatLon } from "./geo";
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

  // Detection arc accumulation buffer: key → {hex, node_id, arc, doppler_hz, target_class, ts}
  // Arcs persist for ARC_MAX_AGE_MS after last update, enabling fade-out per detection.
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
    (newAircraft, groundTruth, groundTruthMeta, anomalyHexes) => {
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

      // Accumulate detection arcs as a radar-style afterglow trail. Each
      // ingest of new data from the backend lays down a new ellipse per
      // (aircraft, node) pair; each ellipse fades on its own clock.  Keying
      // by the ingest timestamp (rather than a measurement value like
      // delay_us) means a stationary aircraft, whose measurement values do
      // not change, still gets a fresh ellipse per backend update — keeping
      // it visibly bright rather than strobing — and a moving aircraft lays
      // down one ellipse per snapshot at slightly different geometry, which
      // is the trail.
      const now = Date.now();
      const ARC_MAX_AGE_MS = ARC_TOTAL_LIFE_MS;
      const buf = arcsBufferRef.current;
      for (const ac of newAircraft) {
        if (Array.isArray(ac.ambiguity_arc) && ac.ambiguity_arc.length >= 2 && ac.node_id) {
          const key = `${ac.hex}-${ac.node_id}-${now}`;
          // Defensive: if two ingests landed in the same millisecond they
          // would collide on key.  Skip rather than overwrite so the
          // existing ellipse keeps fading from its original ts.
          if (key in buf) continue;
          buf[key] = {
            hex: ac.hex,
            node_id: ac.node_id,
            ambiguity_arc: ac.ambiguity_arc,
            // delay_us is the only bistatic parameter we need to rebuild
            // the locus client-side at the icon's current dead-reckoned
            // position; geometry comes from useNodes.
            delay_us: ac.delay_us ?? null,
            doppler_hz: ac.doppler_hz ?? 0,
            target_class: ac.target_class,
            ts: now,
          };
        }
      }
      // Prune arcs older than ARC_MAX_AGE_MS — they will already have
      // faded to zero opacity in the renderer; this keeps the buffer
      // bounded.
      for (const key of Object.keys(buf)) {
        if (now - buf[key].ts > ARC_MAX_AGE_MS) delete buf[key];
      }

      // Detection-presence oracle (consumed by InBeamDiagnostic). Record
      // every (aircraft, node) pair that produced ANY detection this frame:
      // single-node tracks via node_id, multinode solves via every
      // contributing node. Key on ground_truth_hex when present, else hex —
      // the same identity the ground-truth trail is keyed by, so a multinode
      // track (whose own hex is synthetic) still joins to its aircraft.
      const det = detectionsRef.current;
      for (const ac of newAircraft) {
        const hex = ac.ground_truth_hex || ac.hex;
        if (!hex) continue;
        if (ac.node_id) det[`${hex}|${ac.node_id}`] = now;
        if (Array.isArray(ac.contributing_node_ids)) {
          for (const nid of ac.contributing_node_ids) det[`${hex}|${nid}`] = now;
        }
      }
      // Prune on the same TTL as the arc buffer, so a detection that only
      // just expired still counts within the grace window.
      for (const key of Object.keys(det)) {
        if (now - det[key] > ARC_MAX_AGE_MS) delete det[key];
      }

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
        ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes);
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
          ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes);
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
  const [nodes, setNodes] = useState([]);

  useEffect(() => {
    async function loadNodes() {
      try {
        // On map.retina.fm request only real nodes from the backend — avoids
        // relying on client-side hostname detection to filter 900+ synthetic markers.
        const url = usesRealOnlyFeed
          ? `${API_BASE}/radar/analytics?real_only=true`
          : `${API_BASE}/radar/analytics`;
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();
        const nodeList = [];
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
    return () => clearInterval(interval);
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
