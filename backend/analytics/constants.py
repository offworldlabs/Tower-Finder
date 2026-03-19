"""Shared constants and helpers for node analytics."""

import math

C_KM_US = 0.299792458   # speed of light km/μs
R_EARTH = 6371.0         # Earth radius km

# Yagi antenna spec
YAGI_BEAM_WIDTH_DEG = 41.0   # typical 40-42° half-power beamwidth
YAGI_MAX_RANGE_KM = 50.0


def haversine_km(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
