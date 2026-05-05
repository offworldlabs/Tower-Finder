"""Background task: hourly Open-Meteo weather snapshots per connected node.

Once an hour, snapshot ``state.connected_nodes``, fetch current weather from
Open-Meteo for each node's ``rx_lat/rx_lon``, and write one Parquet file per
node under ``coverage_data/weather/``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core import state
from services.weather_client import fetch_current
from services.weather_writer import write_weather_parquet

logger = logging.getLogger(__name__)

WEATHER_FETCH_INTERVAL_S = int(os.getenv("WEATHER_FETCH_INTERVAL_S", str(3600)))

_WEATHER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "coverage_data",
    "weather",
)


def _node_locations() -> list[tuple[str, float, float]]:
    """Snapshot connected_nodes into a list of (node_id, lat, lon).

    Skips entries without a usable rx_lat/rx_lon in the node config. Holds the
    state lock only for the snapshot — network I/O happens unlocked.
    """
    out: list[tuple[str, float, float]] = []
    with state.connected_nodes_lock:
        snapshot = list(state.connected_nodes.items())
    for nid, info in snapshot:
        cfg = (info or {}).get("config") or {}
        lat = cfg.get("rx_lat")
        lon = cfg.get("rx_lon")
        try:
            if lat is None or lon is None:
                continue
            out.append((nid, float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    return out


def fetch_and_write_once(*, write_ts: datetime | None = None) -> dict:
    """Single iteration: fetch + write per-node Parquet files. Returns counts."""
    write_ts = write_ts or datetime.now(timezone.utc)
    fetch_ts_ms = int(write_ts.timestamp() * 1000)
    Path(_WEATHER_DIR).mkdir(parents=True, exist_ok=True)

    stats = {"nodes": 0, "samples": 0, "errors": 0, "files": 0}
    for nid, lat, lon in _node_locations():
        stats["nodes"] += 1
        sample = fetch_current(lat, lon)
        if not sample:
            stats["errors"] += 1
            continue
        sample["node_id"] = nid
        sample["fetch_ts_ms"] = fetch_ts_ms
        try:
            key = write_weather_parquet(
                samples=[sample],
                base_dir=_WEATHER_DIR,
                node_id=nid,
                write_ts=write_ts,
            )
        except Exception:
            logger.exception("Weather write failed for %s", nid)
            stats["errors"] += 1
            continue
        if key:
            stats["files"] += 1
            stats["samples"] += 1
    return stats


async def weather_archive_task():
    """Periodic loop. Runs forever as a background asyncio task."""
    while True:
        await asyncio.sleep(WEATHER_FETCH_INTERVAL_S)
        try:
            stats = await asyncio.get_running_loop().run_in_executor(
                None, fetch_and_write_once
            )
            logger.info("weather archive: %s", stats)
            state.task_last_success["weather_archive"] = time.time()
        except Exception:
            state.task_error_counts["weather_archive"] += 1
            logger.exception("weather_archive_task iteration failed")
