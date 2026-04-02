"""Background async tasks that run for the lifetime of the server.

- frame_processor_loop    – drains state.frame_queue in a thread-pool
- aircraft_flush_task     – writes aircraft.json + broadcasts via WS every 2 s
- analytics_refresh_task  – pre-computes analytics/nodes/overlaps every 30 s
- reputation_evaluator    – re-evaluates node reputations every 60 s
- adsb_truth_fetcher      – fetches OpenSky positions every 30 s
- start_solver_workers    – launches daemon threads that drain solver_queue
"""

import asyncio
import concurrent.futures
import json
import logging
import math
import os
import queue
import threading
import time

import httpx
import orjson

from core import state
from services.frame_processor import (
    build_combined_aircraft_json,
    process_one_frame,
    flush_all_archive_buffers,
    _ARCHIVE_FLUSH_INTERVAL,
)

_TAR1090_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tar1090_data")

# Dedicated single-thread executor for analytics pre-computation so it
# never competes with frame processing in the default pool.
_analytics_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="analytics-bg",
)

# Dedicated single-thread executor for aircraft flush — isolated from the
# default pool used by frame workers so flush is never starved under load.
_aircraft_flush_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="aircraft-flush",
)

# Persistent httpx client reused across OpenSky fetches — preserves connection pooling
_opensky_client: httpx.AsyncClient | None = None


# ── Analytics / nodes / overlaps pre-computation (30 s tick) ──────────────────

def _refresh_analytics_and_nodes():
    """Heavy work: recompute analytics, nodes, and overlaps → store as bytes."""
    from services.tcp_handler import is_synthetic_node

    # Analytics
    analytics_data = {
        "nodes": state.node_analytics.get_all_summaries(),
        "cross_node": state.node_analytics.get_cross_node_analysis(),
    }
    state.latest_analytics_bytes = orjson.dumps(analytics_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Real-only variant: strip synthetic nodes so map.retina.fm never receives them
    real_node_ids = {
        nid for nid, info in state.connected_nodes.items()
        if not info.get("is_synthetic", True)
    }
    analytics_real_data = {
        "nodes": {k: v for k, v in analytics_data["nodes"].items() if k in real_node_ids},
        "cross_node": analytics_data["cross_node"],
    }
    state.latest_analytics_real_bytes = orjson.dumps(analytics_real_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Nodes — snapshot once to avoid RuntimeError from concurrent TCP handler mutations
    _nodes_snapshot = list(state.connected_nodes.items())
    nodes_data = {
        "nodes": {
            nid: {
                "status": info.get("status"),
                "name": info.get("config", {}).get("name", nid),
                "config_hash": info.get("config_hash"),
                "last_heartbeat": info.get("last_heartbeat"),
                "peer": info.get("peer"),
                "is_synthetic": info.get("is_synthetic", is_synthetic_node(nid)),
                "capabilities": info.get("capabilities", {}),
                "frequency": (
                    info.get("config", {}).get("FC")
                    or info.get("config", {}).get("fc_hz")
                    or info.get("config", {}).get("frequency")
                ),
                "sample_rate": (
                    info.get("config", {}).get("Fs")
                    or info.get("config", {}).get("fs_hz")
                ),
                "location": {
                    "rx_lat": info.get("config", {}).get("rx_lat"),
                    "rx_lon": info.get("config", {}).get("rx_lon"),
                    "rx_alt_ft": info.get("config", {}).get("rx_alt_ft"),
                    "tx_lat": info.get("config", {}).get("tx_lat"),
                    "tx_lon": info.get("config", {}).get("tx_lon"),
                    "tx_alt_ft": info.get("config", {}).get("tx_alt_ft"),
                },
            }
            for nid, info in _nodes_snapshot
        },
        "connected": sum(1 for _, n in _nodes_snapshot if n.get("status") not in ("disconnected",)),
        "total": len(_nodes_snapshot),
        "synthetic": sum(1 for _, n in _nodes_snapshot if n.get("is_synthetic")),
    }
    state.latest_nodes_bytes = orjson.dumps(nodes_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Overlaps — only include zones with actual overlap to keep payload small
    overlaps_data = {
        "overlaps": [z for z in state.node_associator.get_overlap_summary() if z["has_overlap"]],
        "registered_nodes": list(state.node_associator.node_geometries.keys()),
    }
    state.latest_overlaps_bytes = orjson.dumps(overlaps_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Solver-vs-ADS-B accuracy statistics
    _refresh_accuracy_stats()

    # Synthetic chain-of-custody entries for connected nodes that lack them
    _ensure_custody_data()
    # Evict PassiveRadarPipeline instances for long-disconnected nodes to free RAM
    _evict_stale_pipelines(_nodes_snapshot)


def _refresh_accuracy_stats():
    """Compute solver-vs-ADS-B accuracy from the rolling sample buffer."""
    samples = list(state.accuracy_samples)
    if not samples:
        state.latest_accuracy_bytes = orjson.dumps({"n_samples": 0})
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

    def _percentile(sorted_vals, pct):
        idx = int(pct / 100 * (len(sorted_vals) - 1))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    # Per-source breakdown
    by_source: dict[str, list[float]] = {}
    for s in samples:
        by_source.setdefault(s["position_source"], []).append(s["error_km"])

    source_stats = {}
    for src, errs in by_source.items():
        errs.sort()
        sn = len(errs)
        source_stats[src] = {
            "n_samples": sn,
            "mean_km": round(sum(errs) / sn, 4),
            "median_km": round(_percentile(errs, 50), 4),
            "p95_km": round(_percentile(errs, 95), 4),
            "max_km": round(errs[-1], 4),
        }

    result = {
        "n_samples": n,
        "mean_km": round(sum(errors) / n, 4),
        "median_km": round(_percentile(errors, 50), 4),
        "p95_km": round(_percentile(errors, 95), 4),
        "max_km": round(errors[-1], 4),
        "by_source": source_stats,
    }
    state.latest_accuracy_bytes = orjson.dumps(result)


def _ensure_custody_data():
    """Auto-register connected nodes in chain-of-custody if they lack entries.

    This ensures the custody dashboard shows data for all fleet nodes.
    """
    import hashlib
    from datetime import datetime, timezone
    from chain_of_custody.models import NodeIdentity

    now_iso = datetime.now(timezone.utc).isoformat()
    hour_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    for nid, info in list(state.connected_nodes.items()):
        if info.get("status") == "disconnected":
            continue

        # Auto-register identity if missing
        if nid not in state.node_identities:
            fingerprint = hashlib.sha256(nid.encode()).hexdigest()[:16]
            identity = NodeIdentity(
                node_id=nid,
                public_key_pem=f"-----SIM-KEY-{nid[-8:]}-----",
                public_key_fingerprint=fingerprint,
                serial_number=f"SIM-{nid[-6:]}",
                signing_mode="software",
                registered_at=now_iso,
            )
            state.node_identities[nid] = identity

        # Add a chain entry per hour if none exists for this hour
        if nid not in state.chain_entries:
            state.chain_entries[nid] = []

        entries = state.chain_entries[nid]
        # Trim to last 168 entries (7 days) to prevent unbounded RAM growth
        if len(entries) > 168:
            state.chain_entries[nid] = entries = entries[-168:]
        if not entries or entries[-1].get("hour_utc") != hour_utc:
            prev_hash = entries[-1].get("entry_hash", "0" * 64) if entries else "0" * 64
            content_hash = hashlib.sha256(f"{nid}:{hour_utc}".encode()).hexdigest()
            entry_hash = hashlib.sha256(f"{prev_hash}:{content_hash}".encode()).hexdigest()
            entries.append({
                "node_id": nid,
                "hour_utc": hour_utc,
                "prev_hash": prev_hash,
                "content_hash": content_hash,
                "entry_hash": entry_hash,
                "_verified": True,
                "_received_at": now_iso,
            })

        # Add IQ commitment if none
        if nid not in state.iq_commitments:
            state.iq_commitments[nid] = []
        if not state.iq_commitments[nid]:
            state.iq_commitments[nid].append({
                "node_id": nid,
                "capture_id": f"iq-{nid[-8:]}-001",
                "sha256": hashlib.sha256(f"iq:{nid}".encode()).hexdigest(),
                "_received_at": now_iso,
            })


def _evict_stale_pipelines(nodes_snapshot: list):
    """Remove PassiveRadarPipeline for nodes disconnected > 2 h. Frees tracker/geolocator RAM."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    stale = []
    for nid, info in nodes_snapshot:
        if info.get("status") != "disconnected":
            continue
        hb = info.get("last_heartbeat")
        if not hb:
            stale.append(nid)
            continue
        try:
            hb_time = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            if (now - hb_time).total_seconds() > 7200:
                stale.append(nid)
        except Exception:
            pass
    for nid in stale:
        state.node_pipelines.pop(nid, None)
    if stale:
        logging.debug("Evicted %d stale node pipelines", len(stale))


async def analytics_refresh_task():
    """Pre-compute analytics/nodes/overlaps every 30 s in a dedicated thread."""
    loop = asyncio.get_event_loop()
    # Initial delay: wait for some nodes to register
    await asyncio.sleep(5)
    while True:
        try:
            await loop.run_in_executor(_analytics_executor, _refresh_analytics_and_nodes)
            # Check for offline nodes and auto-log events
            from routes.admin import check_node_health
            check_node_health()
            logging.debug("Analytics refresh completed")
        except Exception:
            logging.debug("Analytics refresh failed", exc_info=True)
        await asyncio.sleep(30)


# ── Background multinode solver (solver_queue → solver threads) ──────────────

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))


def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread.

    Keeping the solver in its own threads lets scipy/numpy release the GIL and
    use a second core while the frame-ingestion workers stay fast.
    """
    from retina_geolocator.multinode_solver import solve_multinode
    while True:
        try:
            s_in, node_cfgs = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            result = solve_multinode(s_in, node_cfgs)
        except Exception:
            result = None
        if result and result.get("success"):
            # Record the solved position as a calibration point for each
            # contributing node — multinode solutions are high-confidence.
            for nid in result.get("contributing_node_ids", []):
                state.node_analytics.record_calibration_point(
                    nid, result["lat"], result["lon"]
                )
            key = f"mn-{result['timestamp_ms']}-{result['lat']:.3f}"
            state.multinode_tracks[key] = result


def start_solver_workers():
    """Start N daemon threads that continuously drain the solver queue."""
    for i in range(_N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker, daemon=True, name=f"solver-{i}",
        )
        t.start()
    logging.info("Started %d multinode solver worker(s)", _N_SOLVER_WORKERS)


# ── Frame processor (drain queue → thread-pool) ──────────────────────────────

async def frame_processor_loop(default_pipeline):
    """Process queued detection frames sequentially in a thread pool."""
    loop = asyncio.get_event_loop()
    while True:
        node_id, frame = await state.frame_queue.get()
        try:
            await loop.run_in_executor(
                None, process_one_frame, node_id, frame, default_pipeline,
            )
            state.aircraft_dirty = True
        except Exception:
            logging.debug("Frame processing failed", exc_info=True)
        finally:
            state.frame_queue.task_done()
        # Yield to event loop between frames so HTTP/TCP handlers stay responsive
        await asyncio.sleep(0)


# ── Aircraft flush (2 s tick) ─────────────────────────────────────────────────

def _build_real_only_payload(aircraft_data: dict) -> bytes:
    """Build a slim WS payload filtered to non-synthetic nodes only."""
    real_node_ids = {
        nid for nid, info in state.connected_nodes.items()
        if not info.get("is_synthetic", True)
    }
    real_aircraft = [
        ac for ac in aircraft_data.get("aircraft", [])
        if ac.get("node_id") in real_node_ids
        or (ac.get("multinode") and any(
            nid in real_node_ids for nid in ac.get("contributing_node_ids", [])
        ))
    ]
    real_arcs = [
        arc for arc in aircraft_data.get("detection_arcs", [])
        if arc.get("node_id") in real_node_ids
    ]
    payload = {
        "now": aircraft_data.get("now", 0),
        "messages": len(real_aircraft),
        "aircraft": real_aircraft,
        "detection_arcs": real_arcs,
        "ground_truth": {},
        "ground_truth_meta": {},
        "anomaly_hexes": [],
    }
    return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY)


async def broadcast_aircraft(aircraft_data: dict, aircraft_bytes: bytes):
    """Push updated aircraft data to all connected WebSocket clients."""
    state.latest_aircraft_json = aircraft_data
    state.latest_aircraft_json_bytes = aircraft_bytes

    # Pre-compute real-node-only payload for map.retina.fm clients
    real_bytes = _build_real_only_payload(aircraft_data)
    state.latest_real_aircraft_json_bytes = real_bytes

    # Broadcast real-only payload to live clients (map.retina.fm)
    if state.ws_live_clients:
        real_payload = real_bytes.decode()
        stale_live = set()
        for ws in list(state.ws_live_clients):
            try:
                await asyncio.wait_for(ws.send_text(real_payload), timeout=5.0)
            except Exception:
                stale_live.add(ws)
        state.ws_live_clients.difference_update(stale_live)
        for ws in stale_live:
            try:
                await ws.close()
            except Exception:
                pass

    if not state.ws_clients:
        return
    # Build a slim WS payload — ground_truth trails can be 20+ MB (full history
    # of 900+ aircraft) which causes send timeouts and leaves clients connected
    # but receiving nothing.  WS clients only need the last position per GT
    # aircraft for the map overlay; full trails are available via HTTP.
    gt_full = aircraft_data.get("ground_truth") or {}
    gt_slim = {hex_code: [positions[-1]] for hex_code, positions in gt_full.items() if positions}
    slim_data = {**aircraft_data, "ground_truth": gt_slim}
    payload = orjson.dumps(slim_data, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    stale = set()
    for ws in list(state.ws_clients):  # snapshot to avoid set-changed-during-iteration
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
        except Exception:
            stale.add(ws)
    state.ws_clients.difference_update(stale)
    # Close stale connections so the frontend's onclose fires and triggers
    # reconnect (otherwise the socket appears open but receives nothing).
    for ws in stale:
        try:
            await ws.close()
        except Exception:
            pass


async def aircraft_flush_task(default_pipeline):
    """Write aircraft.json to disk and broadcast via WS at ~2 Hz."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(1.0)
        if not state.aircraft_dirty:
            continue
        state.aircraft_dirty = False
        try:
            # Run the heavy iteration in a dedicated thread — isolated from the
            # default pool used by FRAME_WORKERS so flush is never starved.
            def _build_and_serialize():
                data = build_combined_aircraft_json(default_pipeline)
                data_bytes = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
                aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                with open(aircraft_path, "wb") as f:
                    f.write(data_bytes)
                return data, data_bytes
            aircraft_data, aircraft_bytes = await loop.run_in_executor(
                _aircraft_flush_executor, _build_and_serialize,
            )
            await broadcast_aircraft(aircraft_data, aircraft_bytes)
        except Exception:
            logging.exception("Aircraft flush failed")


# ── Reputation evaluator (60 s tick) ─────────────────────────────────────────

async def archive_flush_task():
    """Periodically flush batched detection archives to disk/B2."""
    while True:
        await asyncio.sleep(_ARCHIVE_FLUSH_INTERVAL)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, flush_all_archive_buffers)
        except Exception:
            logging.debug("Archive batch flush failed", exc_info=True)


async def reputation_evaluator():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(60)
        try:
            await loop.run_in_executor(
                None, state.node_analytics.evaluate_reputations,
            )
        except Exception:
            logging.exception("Reputation evaluation failed")


# ── External ADS-B truth fetcher (30 s tick) ─────────────────────────────────

async def adsb_truth_fetcher():
    backoff = 0
    while True:
        await asyncio.sleep(120 + backoff)
        backoff = 0
        try:
            rate_limited = await _fetch_external_adsb()
            if rate_limited:
                backoff = 300  # back off 5 min on 429
        except Exception:
            logging.debug("External ADS-B fetch skipped")


async def _fetch_external_adsb() -> bool:
    """Fetch aircraft positions from OpenSky Network for cross-validation.

    Returns True if rate-limited (HTTP 429), False otherwise.
    """
    active_nodes = [
        info for info in state.connected_nodes.values()
        if info.get("status") != "disconnected" and info.get("config")
    ]
    if not active_nodes:
        return False

    # Skip OpenSky when all connected nodes are synthetic — no real ADS-B to validate against
    if all(info.get("is_synthetic", False) for info in active_nodes):
        logging.debug("All nodes synthetic — skipping OpenSky fetch")
        return False

    lats = [n["config"].get("rx_lat", 0) for n in active_nodes]
    lons = [n["config"].get("rx_lon", 0) for n in active_nodes]

    # Prefer real (non-synthetic) nodes for the bounding box — querying a
    # country-wide box when 900+ synthetic nodes are active causes OpenSky
    # to time out.  If no real nodes exist fall back to all nodes.
    real_nodes = [n for n in active_nodes if not n.get("is_synthetic", False)]
    if real_nodes:
        lats = [n["config"].get("rx_lat", 0) for n in real_nodes]
        lons = [n["config"].get("rx_lon", 0) for n in real_nodes]
    if not lats or all(la == 0 for la in lats):
        return False

    lamin, lamax = min(lats) - 1.0, max(lats) + 1.0
    lomin, lomax = min(lons) - 1.0, max(lons) + 1.0

    url = "https://opensky-network.org/api/states/all"
    global _opensky_client
    if _opensky_client is None or _opensky_client.is_closed:
        _opensky_client = httpx.AsyncClient(timeout=15.0)
    try:
        resp = await _opensky_client.get(url, params={
            "lamin": lamin, "lamax": lamax,
            "lomin": lomin, "lomax": lomax,
        })
        if resp.status_code == 429:
            logging.debug("OpenSky rate-limited (429) — backing off")
            return True
        if resp.status_code != 200:
            return False
        data = resp.json()
    except Exception:
        _opensky_client = None  # reset on error; recreated on next call
        return False

    states = data.get("states", [])
    if not states:
        return False

    now_cache = {}
    for s in states:
        icao = s[0] if s[0] else None
        lon_val, lat_val, alt_val = s[5], s[6], s[7]
        if icao and lat_val is not None and lon_val is not None:
            now_cache[icao] = {
                "lat": lat_val,
                "lon": lon_val,
                "alt_m": alt_val or 0,
                "velocity": s[9] if len(s) > 9 else None,
                "heading": s[10] if len(s) > 10 else None,
            }

    state.external_adsb_cache = now_cache
    logging.debug("External ADS-B: cached %d aircraft positions", len(now_cache))
    _cross_validate_adsb_reports()
    return False


def _cross_validate_adsb_reports():
    """Penalise nodes whose ADS-B reports diverge from OpenSky truth."""
    if not state.external_adsb_cache:
        return
    for node_id, ts_state in state.node_analytics.trust_scores.items():
        if not ts_state.samples:
            continue
        for sample in ts_state.samples[-10:]:
            if not sample.adsb_hex:
                continue
            ext = state.external_adsb_cache.get(sample.adsb_hex.lower())
            if ext is None:
                continue
            dlat = sample.adsb_lat - ext["lat"]
            dlon = sample.adsb_lon - ext["lon"]
            dist_km = math.sqrt(dlat ** 2 + dlon ** 2) * 111.0
            if dist_km > 10.0:
                rep = state.node_analytics.reputations.get(node_id)
                if rep:
                    rep.apply_penalty(
                        0.1,
                        f"ADS-B position mismatch: {sample.adsb_hex} "
                        f"reported {dist_km:.1f}km from external truth",
                    )
                    logging.warning(
                        "Node %s ADS-B mismatch for %s: %.1f km off",
                        node_id, sample.adsb_hex, dist_km,
                    )
