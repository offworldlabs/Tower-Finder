"""Shared helper functions for background tasks."""

import hashlib
import math

from config.constants import C_KM_US as _C_KM_US
from config.constants import DELAY_MATCH_THRESHOLD_US as _DELAY_MATCH_THRESHOLD_US  # noqa: F401 — re-exported
from config.constants import R_EARTH_KM


def multinode_hex_from_key(key: str) -> str:
    """Return deterministic synthetic hex ID for a multinode solve key."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:10]
    return f"mn{digest}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km."""
    R = R_EARTH_KM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(min(1.0, math.sqrt(a)))


def bistatic_delay_us(tx_lat, tx_lon, rx_lat, rx_lon, ac_lat, ac_lon) -> float:
    """Compute bistatic excess delay in µs: (d_ta + d_ar - d_tr) / c."""
    d_ta = haversine_km(tx_lat, tx_lon, ac_lat, ac_lon)
    d_ar = haversine_km(ac_lat, ac_lon, rx_lat, rx_lon)
    d_tr = haversine_km(tx_lat, tx_lon, rx_lat, rx_lon)
    return (d_ta + d_ar - d_tr) / _C_KM_US
