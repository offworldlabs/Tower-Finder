"""Periodic background tasks: archive flush, archive lifecycle, reputation, ADS-B truth fetch."""

import asyncio
import logging
import math
import time

import httpx

from config.constants import (
    ADSB_BACKOFF_S,
    ADSB_TRUTH_INTERVAL_S,
    ARCHIVE_FLUSH_INTERVAL_S,
    ARCHIVE_LIFECYCLE_INTERVAL_S,
    OPENSKY_BUFFER_DEG,
    REPUTATION_INTERVAL_S,
)
from core import state
from services.frame_processor import flush_all_archive_buffers

_opensky_client: httpx.AsyncClient | None = None
_adsb_lol_client: object | None = None


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


async def archive_lifecycle_task():
    """Periodically offload old archives to R2 and delete expired local files."""
    from services.tasks.archive_lifecycle import run_archive_lifecycle

    while True:
        await asyncio.sleep(ARCHIVE_LIFECYCLE_INTERVAL_S)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_archive_lifecycle)
            state.task_last_success["archive_lifecycle"] = time.time()
        except Exception:
            state.task_error_counts["archive_lifecycle"] += 1
            logging.exception("Archive lifecycle failed")


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


async def prune_synthetic_nodes():
    """Periodically remove old synthetic/test nodes that have been disconnected."""
    # Test nodes (e2e-, synth-, test- prefixes) disconnected >7 days old get pruned
    # to avoid accumulating stale state in memory across CI/CD cycles.
    PRUNE_INTERVAL_S = 6 * 3600  # Every 6 hours
    MAX_AGE_DISCONNECTED_S = 7 * 86400  # 7 days
    
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        try:
            now = time.time()
            pruned = []
            with state.connected_nodes_lock:
                to_remove = []
                for node_id, info in state.connected_nodes.items():
                    # Only prune synthetic/test nodes
                    if not any(node_id.startswith(p) for p in ("synth-", "e2e-", "test-")):
                        continue
                    # Only prune if disconnected
                    if info.get("status") != "disconnected":
                        continue
                    # Only prune if old enough
                    first_seen = info.get("first_seen_ts", now)
                    if now - first_seen > MAX_AGE_DISCONNECTED_S:
                        to_remove.append(node_id)
                        pruned.append(node_id)
                for node_id in to_remove:
                    del state.connected_nodes[node_id]
            
            if pruned:
                logging.info("Pruned %d old synthetic nodes: %s", len(pruned), pruned[:5])
            state.task_last_success["prune_synthetic_nodes"] = time.time()
        except Exception:
            state.task_error_counts["prune_synthetic_nodes"] += 1
            logging.exception("Node pruning failed")


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
    """Fetch aircraft positions for cross-validation.

    Primary source: OpenSky Network.
    Fallback: adsb.lol (free, no auth required) when OpenSky rate-limits or fails.
    Returns True if OpenSky was rate-limited (HTTP 429), False otherwise.
    """
    active_nodes = [
        info for info in list(state.connected_nodes.values())
        if info.get("status") != "disconnected" and info.get("config")
    ]
    if not active_nodes:
        return False

    if all(info.get("is_synthetic", False) for info in active_nodes):
        logging.debug("All nodes synthetic — skipping external ADS-B fetch")
        return False

    real_nodes = [n for n in active_nodes if not n.get("is_synthetic", False)]
    source_nodes = real_nodes if real_nodes else active_nodes
    lats = [n["config"].get("rx_lat", 0) for n in source_nodes]
    lons = [n["config"].get("rx_lon", 0) for n in source_nodes]
    if not lats or all(la == 0 for la in lats):
        return False

    lamin, lamax = min(lats) - OPENSKY_BUFFER_DEG, max(lats) + OPENSKY_BUFFER_DEG
    lomin, lomax = min(lons) - OPENSKY_BUFFER_DEG, max(lons) + OPENSKY_BUFFER_DEG
    lat_center = (lamin + lamax) / 2
    lon_center = (lomin + lomax) / 2

    # ── Try OpenSky first ─────────────────────────────────────────────────────
    opensky_failed = False
    rate_limited = False
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
            logging.debug("OpenSky rate-limited (429) — trying adsb.lol fallback")
            rate_limited = True
            opensky_failed = True
        elif resp.status_code != 200:
            opensky_failed = True
        else:
            data = resp.json()
            states = data.get("states", [])
            if states:
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
                logging.debug("OpenSky: cached %d aircraft positions", len(now_cache))
                _cross_validate_adsb_reports()
                return False
            opensky_failed = True
    except Exception:
        _opensky_client = None
        opensky_failed = True

    # ── Fallback: adsb.lol ────────────────────────────────────────────────────
    if opensky_failed:
        try:
            fallback_cache = await _fetch_adsb_lol(lat_center, lon_center)
            if fallback_cache:
                state.external_adsb_cache = fallback_cache
                logging.debug("adsb.lol fallback: cached %d aircraft positions", len(fallback_cache))
                _cross_validate_adsb_reports()
        except Exception:
            logging.warning("adsb.lol fallback also failed — external ADS-B cache may be stale")

    return rate_limited


async def _fetch_adsb_lol(lat: float, lon: float) -> dict:
    """Fetch aircraft positions from adsb.lol centered on lat/lon.

    Returns {hex: {lat, lon, alt_m, velocity, heading}} matching external_adsb_cache format.
    """
    from clients.adsb_lol import AdsbLolClient
    global _adsb_lol_client
    loop = asyncio.get_running_loop()
    area = {"name": "auto", "lat": lat, "lon": lon, "radius_nm": 200}
    if _adsb_lol_client is None:
        _adsb_lol_client = AdsbLolClient([area])
    else:
        # Update area center so the rate-limit cache key stays consistent
        _adsb_lol_client.areas = [area]
    aircraft = await loop.run_in_executor(None, _adsb_lol_client.fetch_all)
    result = {}
    for ac in aircraft:
        h = (ac.get("hex") or "").lower()
        if not h:
            continue
        alt_baro = ac.get("alt_baro", 0)
        alt_m = alt_baro * 0.3048 if isinstance(alt_baro, (int, float)) else 0.0
        result[h] = {
            "lat": ac.get("lat", 0.0),
            "lon": ac.get("lon", 0.0),
            "alt_m": alt_m,
            "velocity": ac.get("gs"),
            "heading": ac.get("track"),
        }
    return result


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
