"""Background task: drain state.track_archive_buffer into Parquet.

The multi-node solver appends successful solves to ``state.track_archive_buffer``
(see services.tasks.solver._process_solver_item). This task wakes every
``TRACK_ARCHIVE_FLUSH_INTERVAL_S`` seconds, snapshots the buffer, and writes
it as a single Parquet file under ``coverage_data/tracks/``.

The same lifecycle that uploads detection archives to R2 (archive_lifecycle)
will also pick up these track files because the lifecycle iterator globs
``*.parquet`` regardless of the parent directory tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from config.constants import TRACK_ARCHIVE_FLUSH_INTERVAL_S
from core import state
from services.track_writer import write_tracks_parquet

logger = logging.getLogger(__name__)

_TRACKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "coverage_data",
    "tracks",
)


def flush_track_archive_buffer() -> str | None:
    """Drain the buffer and write a Parquet file. Returns the key, or None."""
    records: list[dict] = []
    while state.track_archive_buffer:
        try:
            records.append(state.track_archive_buffer.popleft())
        except IndexError:
            break
    if not records:
        return None

    Path(_TRACKS_DIR).mkdir(parents=True, exist_ok=True)
    try:
        return write_tracks_parquet(records=records, base_dir=_TRACKS_DIR)
    except Exception:
        logger.exception("Track archive flush failed (lost %d records)", len(records))
        return None


async def track_flush_task():
    """Periodic flush loop. Runs forever as a background asyncio task."""
    while True:
        await asyncio.sleep(TRACK_ARCHIVE_FLUSH_INTERVAL_S)
        try:
            key = flush_track_archive_buffer()
            if key:
                logger.debug("track archive flushed: %s", key)
            state.task_last_success["track_archive_flush"] = time.time()
        except Exception:
            state.task_error_counts["track_archive_flush"] += 1
            logger.exception("track_flush_task iteration failed")
