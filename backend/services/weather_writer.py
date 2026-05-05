"""Parquet writer for hourly weather observations sampled per node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


SCHEMA = pa.schema([
    ("sample_ts_ms", pa.int64()),
    ("fetch_ts_ms", pa.int64()),
    ("node_id", pa.string()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("temperature_c", pa.float64()),
    ("humidity_pct", pa.float64()),
    ("pressure_hpa", pa.float64()),
    ("precipitation_mm", pa.float64()),
    ("wind_speed_ms", pa.float64()),
    ("wind_dir_deg", pa.float64()),
    ("cloud_cover_pct", pa.float64()),
    ("visibility_m", pa.float64()),
    ("weather_code", pa.int32()),
])


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _flatten(samples: list[dict]) -> dict[str, list]:
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for s in samples:
        cols["sample_ts_ms"].append(_i(s.get("sample_ts_ms")) or 0)
        cols["fetch_ts_ms"].append(_i(s.get("fetch_ts_ms")) or 0)
        cols["node_id"].append(s.get("node_id") or "")
        cols["lat"].append(_f(s.get("lat")) or 0.0)
        cols["lon"].append(_f(s.get("lon")) or 0.0)
        cols["temperature_c"].append(_f(s.get("temperature_c")))
        cols["humidity_pct"].append(_f(s.get("humidity_pct")))
        cols["pressure_hpa"].append(_f(s.get("pressure_hpa")))
        cols["precipitation_mm"].append(_f(s.get("precipitation_mm")))
        cols["wind_speed_ms"].append(_f(s.get("wind_speed_ms")))
        cols["wind_dir_deg"].append(_f(s.get("wind_dir_deg")))
        cols["cloud_cover_pct"].append(_f(s.get("cloud_cover_pct")))
        cols["visibility_m"].append(_f(s.get("visibility_m")))
        cols["weather_code"].append(_i(s.get("weather_code")))
    return cols


def write_weather_parquet(
    *,
    samples: list[dict],
    base_dir: str | Path,
    node_id: str,
    write_ts: datetime | None = None,
) -> str | None:
    """Write a list of weather samples as a single per-node Parquet file."""
    if not samples:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    cols = _flatten(samples)
    if not cols["sample_ts_ms"]:
        return None

    table = pa.table(cols, schema=SCHEMA)

    key = (
        f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}/"
        f"node_id={node_id}/hourly-{write_ts:%H%M%S}.parquet"
    )
    out_path = Path(base_dir) / key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return key
