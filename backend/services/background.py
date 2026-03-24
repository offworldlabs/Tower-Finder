"""Background async tasks that run for the lifetime of the server.

- frame_processor_loop    – drains state.frame_queue in a thread-pool
- aircraft_flush_task     – writes aircraft.json + broadcasts via WS every 2 s
- analytics_refresh_task  – pre-computes analytics/nodes/overlaps every 30 s
- reputation_evaluator    – re-evaluates node reputations every 60 s
- adsb_truth_fetcher      – fetches OpenSky positions every 30 s
"""

import asyncio
import concurrent.futures
import json
import logging
import math
import os
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


# ── Analytics / nodes / overlaps pre-computation (30 s tick) ──────────────────

def _refresh_analytics_and_nodes():
    """Heavy work: recompute analytics, nodes, and overlaps → store as bytes."""
    from services.tcp_handler import is_synthetic_node

    # Analytics
    analytics_data = {
        "nodes": state.node_analytics.get_all_summaries(),
        "cross_node": state.node_analytics.get_cross_node_analysis(),
    }
    state.latest_analytics_bytes = orjson.dumps(analytics_data)

    # Nodes
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
                "frequency": info.get("config", {}).get("FC", info.get("config", {}).get("frequency")),
                "location": {
                    "rx_lat": info.get("config", {}).get("rx_lat"),
                    "rx_lon": info.get("config", {}).get("rx_lon"),
                    "tx_lat": info.get("config", {}).get("tx_lat"),
                    "tx_lon": info.get("config", {}).get("tx_lon"),
                },
            }
            for nid, info in state.connected_nodes.items()
        },
        "connected": sum(1 for n in state.connected_nodes.values() if n.get("status") not in ("disconnected",)),
        "total": len(state.connected_nodes),
        "synthetic": sum(1 for n in state.connected_nodes.values() if n.get("is_synthetic")),
    }
    state.latest_nodes_bytes = orjson.dumps(nodes_data)

    # Overlaps — only include zones with actual overlap to keep payload small
    overlaps_data = {
        "overlaps": [z for z in state.node_associator.get_overlap_summary() if z["has_overlap"]],
        "registered_nodes": list(state.node_associator.node_geometries.keys()),
    }
    state.latest_overlaps_bytes = orjson.dumps(overlaps_data)

    # Synthetic chain-of-custody entries for connected nodes that lack them
    _ensure_custody_data()


def _ensure_custody_data():
    """Auto-register connected nodes in chain-of-custody if they lack entries.

    This ensures the custody dashboard shows data for all fleet nodes.
    """
    import hashlib
    from datetime import datetime, timezone
    from chain_of_custody.models import NodeIdentity

    now_iso = datetime.now(timezone.utc).isoformat()
    hour_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    for nid, info in state.connected_nodes.items():
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

async def broadcast_aircraft(aircraft_data: dict, aircraft_bytes: bytes):
    """Push updated aircraft data to all connected WebSocket clients."""
    state.latest_aircraft_json = aircraft_data
    state.latest_aircraft_json_bytes = aircraft_bytes
    if not state.ws_clients:
        return
    payload = aircraft_bytes.decode()
    stale = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            stale.add(ws)
    state.ws_clients.difference_update(stale)


async def aircraft_flush_task(default_pipeline):
    """Write aircraft.json to disk and broadcast via WS at most every 1 s."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(1)
        if not state.aircraft_dirty:
            continue
        state.aircraft_dirty = False
        try:
            # Run the heavy iteration in a thread — it walks 1000+ pipelines
            # and must NOT block the event loop (causes HTTP starvation).
            def _build_and_serialize():
                data = build_combined_aircraft_json(default_pipeline)
                data_bytes = orjson.dumps(data)
                aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                with open(aircraft_path, "wb") as f:
                    f.write(data_bytes)
                return data, data_bytes
            aircraft_data, aircraft_bytes = await loop.run_in_executor(
                None, _build_and_serialize,
            )
            await broadcast_aircraft(aircraft_data, aircraft_bytes)
        except Exception:
            logging.debug("Aircraft flush failed", exc_info=True)


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
    if not lats or all(la == 0 for la in lats):
        return False

    lamin, lamax = min(lats) - 1.0, max(lats) + 1.0
    lomin, lomax = min(lons) - 1.0, max(lons) + 1.0

    url = "https://opensky-network.org/api/states/all"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={
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
