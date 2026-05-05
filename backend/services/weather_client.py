"""Open-Meteo HTTP client for fetching current weather at a location.

Open-Meteo is a free public weather API with no key requirement and no rate
limits within reasonable bounds (~10k req/day per IP). Docs:
https://open-meteo.com/en/docs

We only use the ``current`` endpoint with a fixed list of variables.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Variables we ask Open-Meteo for. Order matters only for human readability;
# they're returned as a dict keyed by variable name.
_CURRENT_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
    "visibility",
    "weather_code",
)


def _parse_iso_to_ms(s: str | None) -> int:
    """Open-Meteo returns time as ISO without seconds, e.g. '2025-05-05T14:00'."""
    if not s:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    raw = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_current(lat: float, lon: float, *, client: httpx.Client | None = None,
                  timeout: float = 10.0) -> dict[str, Any] | None:
    """Fetch the current weather sample for (lat, lon).

    Returns a dict with the schema fields used by ``services.weather_writer``,
    or None on any failure (network, unexpected payload, etc.). Failures are
    logged at debug level so a transient outage doesn't spam logs.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(_CURRENT_VARS),
        "wind_speed_unit": "ms",
        "timeformat": "iso8601",
        "timezone": "UTC",
    }
    try:
        if client is not None:
            r = client.get(OPEN_METEO_URL, params=params, timeout=timeout)
        else:
            r = httpx.get(OPEN_METEO_URL, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.debug("Open-Meteo fetch failed for (%.4f, %.4f)", lat, lon, exc_info=True)
        return None

    cur = data.get("current") or {}
    if not cur:
        return None

    return {
        "sample_ts_ms": _parse_iso_to_ms(cur.get("time")),
        "lat": lat,
        "lon": lon,
        "temperature_c": cur.get("temperature_2m"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "pressure_hpa": cur.get("surface_pressure"),
        "precipitation_mm": cur.get("precipitation"),
        "wind_speed_ms": cur.get("wind_speed_10m"),
        "wind_dir_deg": cur.get("wind_direction_10m"),
        "cloud_cover_pct": cur.get("cloud_cover"),
        "visibility_m": cur.get("visibility"),
        "weather_code": cur.get("weather_code"),
    }
