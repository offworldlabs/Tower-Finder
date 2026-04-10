"""Periodic background tasks: archive flush, reputation, ADS-B truth fetch."""

import asyncio
import logging
import math
import time

import httpx

from core import state
from config.constants import (
    ARCHIVE_FLUSH_INTERVAL_S,
    REPUTATION_INTERVAL_S,
    ADSB_TRUTH_INTERVAL_S,
    ADSB_BACKOFF_S,
    OPENSKY_BUFFER_DEG,
)
from services.frame_processor import flush_all_archive_buffers

_opensky_client: httpx.AsyncClient | None = None


async def archive_flush_task():
    """Periodically flush batched detection archives to disk/B2."""
    while True:
        await asyncio.sleep(ARCHIVE_FLUSH_INTERVAL_S)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, flush_all_archive_buffers)
            state.task_last_success["archive_flush"] = time.time()
        except Exception:
            state.task_error_counts["archive_flush"] += 1
            logging.exception("Archive batch flush failed")


async def reputation_evaluator():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(REPUTATION_INTERVAL_S)
        try:
            await loop.run_in_executor(
                None, state.node_analytics.evaluate_reputations,
            )
            state.task_last_success["reputation_evaluator"] = time.time()
        except Exception:
            state.task_error_counts["reputation_evaluator"] += 1
            logging.exception("Reputation evaluation failed")


async def adsb_truth_fetcher():
    backoff = 0
    while True:
        await asyncio.sleep(ADSB_TRUTH_INTERVAL_S + backoff)
        backoff = 0
        try:
            rate_limited = await _fetch_external_adsb()
            if rate_limited:
                backoff = ADSB_BACKOFF_S
            state.task_last_success["adsb_truth_fetcher"] = time.time()
        except Exception:
            state.task_error_counts["adsb_truth_fetcher"] += 1
            logging.exception("External ADS-B fetch failed")


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

    if all(info.get("is_synthetic", False) for info in active_nodes):
        logging.debug("All nodes synthetic — skipping OpenSky fetch")
        return False

    lats = [n["config"].get("rx_lat", 0) for n in active_nodes]
    lons = [n["config"].get("rx_lon", 0) for n in active_nodes]

    real_nodes = [n for n in active_nodes if not n.get("is_synthetic", False)]
    if real_nodes:
        lats = [n["config"].get("rx_lat", 0) for n in real_nodes]
        lons = [n["config"].get("rx_lon", 0) for n in real_nodes]
    if not lats or all(la == 0 for la in lats):
        return False

    lamin, lamax = min(lats) - OPENSKY_BUFFER_DEG, max(lats) + OPENSKY_BUFFER_DEG
    lomin, lomax = min(lons) - OPENSKY_BUFFER_DEG, max(lons) + OPENSKY_BUFFER_DEG

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
        _opensky_client = None
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
