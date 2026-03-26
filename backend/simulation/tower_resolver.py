"""Persistent real-tower lookup for synthetic fleet nodes.

For each node in the fleet, this resolver queries the FCC broadcast API
to find the best real transmitter near that node's RX coordinates, then
caches the result so subsequent starts don't need to hit the API at all.

Only new nodes (those not yet in the cache) trigger API calls.  Nodes that
are removed from the fleet are simply ignored — stale cache entries cause no
harm and are cheap to store.

Cache file: ``<simulation_dir>/tower_assignments.json``

Cache format::

    {
        "node_id": {
            "tx_lat": float,
            "tx_lon": float,
            "tx_alt_ft": float,
            "fc_hz": float,
            "tx_callsign": str
        },
        ...
    }

Usage (from orchestrator or CLI)::

    assignments = resolve_towers(fleet_nodes)
    for node in fleet_nodes:
        if node["node_id"] in assignments:
            node.update(assignments[node["node_id"]])
"""

import asyncio
import json
import logging
import math
import os
import sys
import time

log = logging.getLogger(__name__)

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "tower_assignments.json")
_LOOKUP_RADIUS_KM = 80
_MIN_FREQ_HZ = 80_000_000   # ignore sub-80 MHz (below FM band — not useful for PR)
_MAX_FREQ_HZ = 900_000_000  # ignore > 900 MHz (above UHF TV)


def _load_cache() -> dict:
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            log.warning("Could not load tower assignments cache; starting fresh.")
    return {}


def _save_cache(cache: dict) -> None:
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, _CACHE_PATH)


def _m_to_ft(metres: float | None) -> float:
    if metres is None:
        return 1000.0
    return round(metres * 3.28084, 1)


async def _lookup_best_tower(rx_lat: float, rx_lon: float) -> dict | None:
    """Query FCC + Maprad for the best VHF/UHF broadcast tower near the RX site.

    Returns a dict with tx_lat/tx_lon/tx_alt_ft/fc_hz/tx_callsign, or None if
    no suitable tower is found within the search radius.
    """
    # Import here so this module can be imported without the full server env
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    try:
        from clients.fcc import fetch_fcc_broadcast_systems
        from services.tower_ranking import process_and_rank
    except ImportError as exc:
        log.error("Could not import tower clients: %s", exc)
        return None

    try:
        raw = await fetch_fcc_broadcast_systems(rx_lat, rx_lon, radius_km=_LOOKUP_RADIUS_KM)
    except Exception as exc:
        log.warning("FCC lookup failed for (%.4f, %.4f): %s", rx_lat, rx_lon, exc)
        return None

    if not raw:
        return None

    towers = process_and_rank(raw, rx_lat, rx_lon, limit=20, radius_km=_LOOKUP_RADIUS_KM)
    if not towers:
        return None

    # Pick the highest-ranked tower whose frequency is in a useful PR band
    for t in towers:
        freq_hz = (t.get("frequency_mhz") or 0) * 1_000_000
        if not (_MIN_FREQ_HZ <= freq_hz <= _MAX_FREQ_HZ):
            continue
        tx_lat = t.get("latitude")
        tx_lon = t.get("longitude")
        if tx_lat is None or tx_lon is None:
            continue
        alt_m = t.get("altitude_m") or t.get("elevation_m")
        return {
            "tx_lat": round(float(tx_lat), 6),
            "tx_lon": round(float(tx_lon), 6),
            "tx_alt_ft": _m_to_ft(alt_m),
            "fc_hz": round(freq_hz),
            "tx_callsign": (t.get("callsign") or "").strip(),
        }
    return None


async def _resolve_batch(nodes: list[dict], cache: dict, concurrency: int = 8) -> dict:
    """Look up towers for all nodes not already in the cache."""
    pending = [n for n in nodes if n["node_id"] not in cache]
    if not pending:
        return cache

    log.info("Looking up real towers for %d new node(s) (concurrency=%d)…", len(pending), concurrency)

    sem = asyncio.Semaphore(concurrency)
    updated = 0

    async def _resolve_one(node: dict):
        nonlocal updated
        async with sem:
            result = await _lookup_best_tower(node["rx_lat"], node["rx_lon"])
            if result:
                cache[node["node_id"]] = result
                updated += 1
                log.debug("  %s → %s @ %.1f MHz",
                          node["node_id"], result["tx_callsign"],
                          result["fc_hz"] / 1e6)
            else:
                # No real tower found — leave the generated default in place
                log.debug("  %s — no real tower found, keeping generated TX", node["node_id"])
            # Small delay to avoid hammering the FCC API
            await asyncio.sleep(0.15)

    await asyncio.gather(*[_resolve_one(n) for n in pending])
    log.info("Tower lookup complete: %d resolved, %d not found.", updated, len(pending) - updated)
    return cache


def resolve_towers(fleet_nodes: list[dict], cache_path: str = _CACHE_PATH) -> dict:
    """Resolve and cache real TX tower assignments for a fleet.

    Args:
        fleet_nodes: List of node config dicts (output of ``generate_fleet()``).
        cache_path:  Override the default cache file path (mainly for tests).

    Returns:
        ``dict[node_id, {tx_lat, tx_lon, tx_alt_ft, fc_hz, tx_callsign}]``
        containing only the nodes for which a real tower was found.  Apply to
        the fleet with::

            for node in fleet_nodes:
                assignment = result.get(node["node_id"])
                if assignment:
                    node.update(assignment)
    """
    global _CACHE_PATH
    if cache_path != _CACHE_PATH:
        _CACHE_PATH = cache_path

    cache = _load_cache()
    needs_lookup = [n for n in fleet_nodes if n["node_id"] not in cache]

    if not needs_lookup:
        log.info("Tower cache is up to date (%d entries); skipping API calls.", len(cache))
        return cache

    # Run async lookup synchronously (works whether or not there's a running loop)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside an existing async context — use run_until_complete can't work here;
            # create a new thread-loop pair
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    lambda: asyncio.run(_resolve_batch(fleet_nodes, cache))
                )
                cache = future.result(timeout=300)
        else:
            cache = loop.run_until_complete(_resolve_batch(fleet_nodes, cache))
    except RuntimeError:
        cache = asyncio.run(_resolve_batch(fleet_nodes, cache))

    _save_cache(cache)
    return cache


def apply_tower_assignments(fleet_nodes: list[dict], assignments: dict) -> int:
    """Overwrite TX fields in fleet_nodes in-place from cached assignments.

    Returns the number of nodes updated.
    """
    count = 0
    for node in fleet_nodes:
        assignment = assignments.get(node["node_id"])
        if assignment:
            node["tx_lat"] = assignment["tx_lat"]
            node["tx_lon"] = assignment["tx_lon"]
            node["tx_alt_ft"] = assignment["tx_alt_ft"]
            node["fc_hz"] = assignment["fc_hz"]
            node["tx_callsign"] = assignment["tx_callsign"]
            count += 1
    return count
