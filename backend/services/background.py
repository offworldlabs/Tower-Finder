"""Background async tasks that run for the lifetime of the server.

- frame_processor_loop  – drains state.frame_queue in a thread-pool
- aircraft_flush_task   – writes aircraft.json + broadcasts via WS every 2 s
- reputation_evaluator  – re-evaluates node reputations every 60 s
- adsb_truth_fetcher    – fetches OpenSky positions every 30 s
"""

import asyncio
import json
import logging
import math
import os
import time

import httpx

from core import state
from services.frame_processor import (
    build_combined_aircraft_json,
    process_one_frame,
    flush_all_archive_buffers,
    _ARCHIVE_FLUSH_INTERVAL,
)

_TAR1090_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tar1090_data")


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

async def broadcast_aircraft(aircraft_data: dict):
    """Push updated aircraft data to all connected WebSocket clients."""
    state.latest_aircraft_json = aircraft_data
    if not state.ws_clients:
        return
    payload = json.dumps(aircraft_data)
    stale = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            stale.add(ws)
    state.ws_clients.difference_update(stale)


async def aircraft_flush_task(default_pipeline):
    """Write aircraft.json to disk and broadcast via WS at most every 2 s."""
    while True:
        await asyncio.sleep(2)
        if not state.aircraft_dirty:
            continue
        state.aircraft_dirty = False
        try:
            aircraft_data = build_combined_aircraft_json(default_pipeline)
            state.latest_aircraft_json = aircraft_data
            aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
            with open(aircraft_path, "w") as f:
                json.dump(aircraft_data, f)
            await broadcast_aircraft(aircraft_data)
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
    while True:
        await asyncio.sleep(60)
        try:
            state.node_analytics.evaluate_reputations()
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
