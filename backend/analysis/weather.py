"""On-demand historical weather fetch for joining against detection data.

We deliberately do NOT ship a copy of weather observations alongside the
detection archive — Open-Meteo offers a free archive API
(https://archive-api.open-meteo.com/v1/archive) that returns the same
hourly data given (lat, lon, date range). Storing it ourselves would mean
maintaining a background task, partition layout, and a copy that drifts
from the upstream source if Open-Meteo revises observations later.

Use this helper from analysis notebooks / pipelines to enrich detections
with weather at query time:

    from datetime import datetime, timezone
    from analysis.weather import fetch_historical

    rows = fetch_historical(
        lat=40.7, lon=-74.0,
        start_dt=datetime(2025, 6, 1, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 2, tzinfo=timezone.utc),
    )
    # rows -> [{"sample_ts_ms": ..., "lat": ..., "temperature_c": ..., ...}, ...]
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"

# Hourly variables we fetch — same shape as the field names used elsewhere
# in the project so analyst code can rely on a single column vocabulary.
_HOURLY_VARS: tuple[tuple[str, str], ...] = (
    # (open-meteo name, output column name)
    ("temperature_2m", "temperature_c"),
    ("relative_humidity_2m", "humidity_pct"),
    ("surface_pressure", "pressure_hpa"),
    ("precipitation", "precipitation_mm"),
    ("wind_speed_10m", "wind_speed_ms"),
    ("wind_direction_10m", "wind_dir_deg"),
    ("cloud_cover", "cloud_cover_pct"),
    ("visibility", "visibility_m"),
    ("weather_code", "weather_code"),
)


def _parse_iso_to_ms(s: str) -> int:
    """Open-Meteo returns time as ISO without seconds, e.g. '2025-05-05T14:00'."""
    raw = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_historical(
    lat: float,
    lon: float,
    start_dt: datetime,
    end_dt: datetime,
    *,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch hourly historical weather for ``[start_dt, end_dt]`` at (lat, lon).

    Returns a list of per-hour dicts. Empty list on any failure (network,
    rate-limit, malformed payload, etc.) — callers should treat absence of
    data as "weather unavailable for this window," not an exception.

    Both ``start_dt`` and ``end_dt`` are interpreted as UTC. Naive datetimes
    are treated as already-UTC.
    """
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    if end_dt < start_dt:
        return []

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_dt.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "end_date": end_dt.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "hourly": ",".join(name for name, _ in _HOURLY_VARS),
        "wind_speed_unit": "ms",
        "timeformat": "iso8601",
        "timezone": "UTC",
    }
    try:
        if client is not None:
            r = client.get(ARCHIVE_API_URL, params=params, timeout=timeout)
        else:
            r = httpx.get(ARCHIVE_API_URL, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.debug(
            "Open-Meteo archive fetch failed for (%.4f, %.4f) %s..%s",
            lat, lon, params["start_date"], params["end_date"], exc_info=True,
        )
        return []

    hourly = data.get("hourly") or {}
    times: list[str] = hourly.get("time") or []
    if not times:
        return []

    rows: list[dict[str, Any]] = []
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    for i, t in enumerate(times):
        try:
            ts_ms = _parse_iso_to_ms(t)
        except ValueError:
            continue
        if ts_ms < start_ms or ts_ms > end_ms:
            continue
        row: dict[str, Any] = {
            "sample_ts_ms": ts_ms,
            "lat": lat,
            "lon": lon,
        }
        for src_name, out_name in _HOURLY_VARS:
            arr = hourly.get(src_name) or []
            row[out_name] = arr[i] if i < len(arr) else None
        rows.append(row)
    return rows
