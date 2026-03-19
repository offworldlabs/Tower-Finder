import json
import math
import os
import re

EARTH_RADIUS_KM = 6371.0
FREQUENCY_MATCH_TOLERANCE_MHZ = 5.0  # ±5 MHz for user-measured frequency matching

# ── Load configurable settings from tower_config.json ────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "tower_config.json")


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


def reload_config():
    """Re-read tower_config.json and update module-level settings."""
    global RX_ANTENNA_GAIN_DBI, SENSITIVITY_DBM
    global BROADCAST_BANDS, BAND_PRIORITY
    global DISTANCE_CLASSES, DISTANCE_PRIORITY, SORT_ORDER
    global DEFAULT_RADIUS_KM, DEFAULT_LIMIT

    cfg = _load_config()

    rx = cfg.get("receiver", {})
    RX_ANTENNA_GAIN_DBI = rx.get("rx_antenna_gain_dbi", 6.0)
    SENSITIVITY_DBM = rx.get("sensitivity_dbm", -95.0)

    BROADCAST_BANDS = {
        band: [tuple(r) for r in ranges]
        for band, ranges in cfg.get("broadcast_bands", {}).items()
    }

    ranking = cfg.get("ranking", {})
    BAND_PRIORITY = ranking.get("band_priority", {"VHF": 0, "UHF": 1, "FM": 2})

    DISTANCE_CLASSES = []
    for dc in ranking.get("distance_classes", []):
        max_km = dc["max_km"] if dc["max_km"] is not None else float("inf")
        DISTANCE_CLASSES.append((dc["label"], dc["min_km"], max_km))

    DISTANCE_PRIORITY = ranking.get("distance_priority", {})
    SORT_ORDER = ranking.get("sort_order", [
        {"field": "band_priority", "ascending": True},
        {"field": "distance_priority", "ascending": True},
        {"field": "received_power_dbm", "ascending": False},
    ])

    search = cfg.get("search", {})
    DEFAULT_RADIUS_KM = search.get("default_radius_km", 80)
    DEFAULT_LIMIT = search.get("default_limit", 20)


# Initialise on import
reload_config()


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two points."""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing in degrees (0-360) from point 1 to point 2."""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_to_cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(deg / 22.5) % 16
    return dirs[ix]


def fspl(distance_km: float, freq_mhz: float) -> float:
    """Free-space path loss in dB."""
    if distance_km <= 0 or freq_mhz <= 0:
        return 0.0
    d_m = distance_km * 1000
    f_hz = freq_mhz * 1e6
    return 20 * math.log10(d_m) + 20 * math.log10(f_hz) - 147.55


def received_power(eirp_dbm: float, distance_km: float, freq_mhz: float) -> float:
    """Estimated received power (dBm) at a small directional antenna."""
    return eirp_dbm + RX_ANTENNA_GAIN_DBI - fspl(distance_km, freq_mhz)


def classify_band(freq_mhz: float) -> str | None:
    for band, ranges in BROADCAST_BANDS.items():
        for lo, hi in ranges:
            if lo <= freq_mhz <= hi:
                return band
    return None


def classify_distance(distance_km: float) -> str:
    for label, lo, hi in DISTANCE_CLASSES:
        if lo <= distance_km < hi:
            return label
    return "Far"


def watts_to_dbm(watts: float) -> float:
    """Convert watts to dBm. Returns -inf for zero/negative input."""
    if watts <= 0:
        return float("-inf")
    return 10 * math.log10(watts) + 30


def eirp_dbm_from_device(device: dict) -> float | None:
    """
    Extract or estimate EIRP in dBm from a device record.
    NOTE: Maprad stores power values in watts regardless of requested unit.
    """
    eirp = device.get("eirp")
    if eirp is not None:
        val = _as_float(eirp)
        if val is not None and val > 0:
            return watts_to_dbm(val)

    tp = device.get("transmitPower")
    gain = (device.get("antenna") or {}).get("gain")
    if tp is not None:
        tp_val = _as_float(tp)
        if tp_val is not None and tp_val > 0:
            tp_dbm = watts_to_dbm(tp_val)
            # antenna gain is in dBi
            antenna_gain = gain if gain is not None else 10.0
            return tp_dbm + antenna_gain

    return None


def _as_float(val) -> float | None:
    """Coerce a scalar value that might be float, int, string, or dict."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    if isinstance(val, dict):
        # FloatValueBlock might have a 'value' or 'low'/'high' key
        if "value" in val:
            return float(val["value"])
        if "low" in val and "high" in val:
            return (float(val["low"]) + float(val["high"])) / 2
    return None


def parse_geom(geom) -> tuple[float, float] | None:
    """
    Extract (latitude, longitude) from a Maprad geom field.
    Handles both POINT and POLYGON/MULTIPOLYGON (uses centroid).
    The API returns geom as {"string": "WKT"} dict.
    """
    if not geom:
        return None
    # The API wraps the WKT in a {"string": "..."} object
    if isinstance(geom, dict):
        geom = geom.get("string") or geom.get("wkt") or ""
    if not isinstance(geom, str) or not geom.strip():
        return None

    wkt = geom.strip().upper()

    if wkt.startswith("POINT"):
        inner = geom[geom.index("(") + 1 : geom.index(")")]
        parts = inner.split()
        if len(parts) >= 2:
            return float(parts[1]), float(parts[0])  # WKT is lng lat
        return None

    # For polygons / multipolygons, compute centroid from the first ring
    if "POLYGON" in wkt:
        return _polygon_centroid(geom)

    return None


def _polygon_centroid(wkt: str) -> tuple[float, float] | None:
    """Rough centroid: average of all coordinate pairs in the first ring."""
    # Find the first parenthesized coordinate sequence
    # MULTIPOLYGON has triple parens, POLYGON has double
    match = re.search(r"\(\([\(]?([-\d\.\s,]+)\)?", wkt)
    if not match:
        return None
    coords_str = match.group(1)
    lats, lngs = [], []
    for pair in coords_str.split(","):
        parts = pair.strip().split()
        if len(parts) >= 2:
            try:
                lngs.append(float(parts[0]))
                lats.append(float(parts[1]))
            except ValueError:
                continue
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lngs) / len(lngs)


def parse_user_frequencies(raw: str, max_count: int = 10) -> list[float]:
    """Parse a comma-separated string of frequencies in MHz. Returns up to max_count valid values."""
    if not raw or not raw.strip():
        return []
    freqs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = float(part)
            if 0 < val < 10000:  # reasonable MHz range
                freqs.append(val)
        except ValueError:
            continue
        if len(freqs) >= max_count:
            break
    return freqs


def process_and_rank(raw_systems: list, user_lat: float, user_lon: float, limit: int = 0, user_frequencies: list[float] | None = None, radius_km: float = 0) -> list:
    """
    Takes raw system records from Maprad, filters and ranks them
    for passive radar suitability.

    Args:
        limit: Max towers to return. 0 means use DEFAULT_LIMIT from config.
        radius_km: Search radius in km. Towers beyond this are excluded.
                   0 means use DEFAULT_RADIUS_KM.
    """
    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT
    towers = []

    for system in raw_systems:
        licence = system.get("licence") or {}
        for device in system.get("devices") or []:
            freq_val = _as_float(device.get("frequency"))
            if freq_val is None:
                continue

            band = classify_band(freq_val)
            if band is None:
                continue  # not in a broadcast band

            loc = device.get("location") or {}
            coords = parse_geom(loc.get("geom"))
            if coords is None:
                continue

            tower_lat, tower_lon = coords
            dist = haversine(user_lat, user_lon, tower_lat, tower_lon)

            # Filter by search radius
            if dist > effective_radius:
                continue

            eirp = eirp_dbm_from_device(device)
            if eirp is None:
                # Reasonable default for a broadcast tower
                eirp = 50.0 if band == "FM" else 60.0

            pwr = received_power(eirp, dist, freq_val)
            if pwr < SENSITIVITY_DBM:
                continue

            brg = initial_bearing(user_lat, user_lon, tower_lat, tower_lon)
            dist_class = classify_distance(dist)

            # Check if tower frequency matches any user-measured frequency (±5 MHz)
            freq_matched = False
            if user_frequencies:
                for uf in user_frequencies:
                    if abs(freq_val - uf) <= FREQUENCY_MATCH_TOLERANCE_MHZ:
                        freq_matched = True
                        break

            towers.append({
                "callsign": device.get("callsign") or "",
                "name": loc.get("name") or "",
                "state": loc.get("state") or "",
                "frequency_mhz": round(freq_val, 3),
                "band": band,
                "latitude": round(tower_lat, 6),
                "longitude": round(tower_lon, 6),
                "antenna_height_m": device.get("antennaHeight"),
                "distance_km": round(dist, 1),
                "bearing_deg": round(brg, 1),
                "bearing_cardinal": bearing_to_cardinal(brg),
                "received_power_dbm": round(pwr, 1),
                "distance_class": dist_class,
                "eirp_dbm": round(eirp, 1),
                "licence_type": licence.get("type") or "",
                "licence_subtype": licence.get("subtype") or "",
                "frequency_matched": freq_matched,
            })

    # Deduplicate by (callsign, frequency) — keep the strongest
    seen = {}
    for t in towers:
        key = (t["callsign"], t["frequency_mhz"])
        if key not in seen or t["received_power_dbm"] > seen[key]["received_power_dbm"]:
            seen[key] = t
    towers = list(seen.values())

    # Sort using configurable sort order
    # If user frequencies provided, frequency-matched towers sort first
    has_user_freqs = bool(user_frequencies)

    def _sort_key(t):
        parts = []
        # Frequency match is highest priority when user provided frequencies
        if has_user_freqs:
            parts.append(0 if t.get("frequency_matched") else 1)
        for rule in SORT_ORDER:
            field = rule["field"]
            asc = rule.get("ascending", True)
            if field == "band_priority":
                val = BAND_PRIORITY.get(t["band"], 99)
            elif field == "distance_priority":
                val = DISTANCE_PRIORITY.get(t["distance_class"], 99)
            else:
                val = t.get(field, 0)
            parts.append(val if asc else -val)
        return tuple(parts)

    towers.sort(key=_sort_key)

    # Assign ranks
    for i, t in enumerate(towers[:effective_limit], 1):
        t["rank"] = i

    return towers[:effective_limit]
