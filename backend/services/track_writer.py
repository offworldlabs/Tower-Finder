"""Parquet writer for multi-node solver track outputs.

Tracks are the *product* of the system — the lat/lon/alt/velocity solutions
emitted by the multi-node solver — as opposed to the raw detections that fed
them. We persist them in their own Parquet stream so the dataset captures
what the system actually output at a given solver version, not just what we
*could* re-derive from raw frames.

Layout:

    tracks/year=YYYY/month=MM/day=DD/part-HHMMSS.parquet

Schema is one row per track update, similar in spirit to the detections schema.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


SCHEMA = pa.schema([
    ("solve_ts_ms", pa.int64()),
    ("frame_ts_ms", pa.int64()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("alt_m", pa.float64()),
    ("vel_east_ms", pa.float64()),
    ("vel_north_ms", pa.float64()),
    ("vel_up_ms", pa.float64()),
    ("n_nodes", pa.int32()),
    ("contributing_node_ids", pa.string()),
    ("adsb_hex", pa.string()),
    ("rms_delay_us", pa.float64()),
    ("rms_doppler_hz", pa.float64()),
    ("target_class", pa.string()),
])


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(d: dict, key: str) -> int | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _flatten(records: list[dict]) -> dict[str, list]:
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for r in records:
        contributing = r.get("contributing_node_ids") or []
        if isinstance(contributing, list):
            contributing = ",".join(str(x) for x in contributing)
        cols["solve_ts_ms"].append(_i(r, "solve_ts_ms") or 0)
        cols["frame_ts_ms"].append(_i(r, "timestamp_ms") or 0)
        cols["lat"].append(_f(r, "lat") or 0.0)
        cols["lon"].append(_f(r, "lon") or 0.0)
        cols["alt_m"].append(_f(r, "alt_m"))
        cols["vel_east_ms"].append(_f(r, "vel_east"))
        cols["vel_north_ms"].append(_f(r, "vel_north"))
        cols["vel_up_ms"].append(_f(r, "vel_up"))
        cols["n_nodes"].append(_i(r, "n_nodes") or 0)
        cols["contributing_node_ids"].append(contributing)
        cols["adsb_hex"].append(r.get("adsb_hex") or None)
        cols["rms_delay_us"].append(_f(r, "rms_delay"))
        cols["rms_doppler_hz"].append(_f(r, "rms_doppler"))
        cols["target_class"].append(r.get("target_class") or None)
    return cols


def write_tracks_parquet(
    *,
    records: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
) -> str | None:
    """Write a list of track records as a single Parquet file.

    Returns the relative key (Hive-partitioned path) or None when ``records``
    is empty.
    """
    if not records:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    cols = _flatten(records)
    if not cols["solve_ts_ms"]:
        return None

    table = pa.table(cols, schema=SCHEMA)

    key = (
        f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}/"
        f"part-{write_ts:%H%M%S}.parquet"
    )
    out_path = Path(base_dir) / key
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return key
