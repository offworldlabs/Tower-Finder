"""Shared helper functions for background tasks."""

import math

_C_KM_US = 0.299792  # speed of light in km/µs
_DELAY_MATCH_THRESHOLD_US = 15.0  # ±15 µs ≈ ±4.5 km path-length tolerance


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km."""
    R = 6371.0
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
