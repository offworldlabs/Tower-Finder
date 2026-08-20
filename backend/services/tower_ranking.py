import json
import logging
import math
import re

from core.runtime_config import default_source_path, migrate_defaults_into_runtime, runtime_path
from services.geo import bearing_deg, haversine_km

logger = logging.getLogger(__name__)

FREQUENCY_MATCH_TOLERANCE_MHZ = 5.0  # ±5 MHz for user-measured frequency matching

# ── Load configurable settings from tower_config.json ────────────────────
_CONFIG_PATH = runtime_path("tower_config.json")


def _read_config(path) -> dict:
    """Read and parse one config file. Raises OSError or ValueError on failure."""
    with path.open() as f:
        return json.load(f)


def _load_config() -> dict:
    # Self-heal: this module is imported at app startup BEFORE the lifespan
    # hook runs, and also imported standalone by tests. If the runtime overlay
    # hasn't been seeded yet, seed it now so the read below finds a file.
    if not _CONFIG_PATH.exists():
        migrate_defaults_into_runtime()
    return _read_config(_CONFIG_PATH)


def _default_config() -> dict | None:
    """The shipped defaults as a dict, or None if no readable copy exists."""
    source = default_source_path("tower_config.json")
    if source is None:
        return None
    try:
        return _read_config(source)
    except (OSError, ValueError):
        logger.error("tower_config: shipped default %s is unreadable", source, exc_info=True)
        return None


def _is_number(value) -> bool:
    # bool subclasses int, but a JSON true where a number belongs is a mistake.
    #
    # NaN and ±Infinity are rejected too. json.loads accepts those bare literals,
    # and every comparison against NaN is False, so an unfiltered NaN passes each
    # range check below and is persisted. It surfaces much later and far from
    # here: DEFAULT_LIMIT = nan makes towers[:effective_limit] raise TypeError on
    # every search, and a NaN distance-class bound silently matches nothing.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# Fields a ranking.sort_order rule may name. _sort_key() negates the value when
# a rule is descending and puts it in a tuple that towers are sorted on, so a
# field only belongs here if it resolves to a real number for every tower.
# band_priority and distance_priority are special-cased there and always do; the
# rest are numeric keys of the tower dict built in process_and_rank(), plus
# coverage_area_added_km2, which services/tower_coverage.py adds, and
# frequency_matched, which is always a bool and so negates cleanly. Deliberately
# absent: the string fields (callsign, name, state, band, bearing_cardinal,
# distance_class, licence_*), and antenna_height_m, which is None whenever the
# upstream record omits it and breaks the sort in either direction.
#
# Adding a numeric field to the tower dict means adding it here too, or a config
# naming it is rejected.
_SORTABLE_FIELDS = frozenset(
    {
        "band_priority",
        "distance_priority",
        "coverage_area_added_km2",
        "received_power_dbm",
        "distance_km",
        "bearing_deg",
        "frequency_mhz",
        "eirp_dbm",
        "frequency_matched",
        "latitude",
        "longitude",
    }
)


def validate_config(cfg: dict) -> str | None:
    """Return an error message if the tower config is unusable, else None.

    Covers exactly what apply_config() consumes, so anything accepted here can
    be applied without raising. Every section is optional — each has a default
    below — but a section that is present must have the right shape. Same
    contract as _validate_node_config in services/tcp_handler.py, which guards
    the TCP path; the shapes differ because that one validates a node's config
    and this one the server's.
    """
    if not isinstance(cfg, dict):
        return f"config must be an object, got {type(cfg).__name__}"

    for section in ("receiver", "ranking", "search", "broadcast_bands"):
        value = cfg.get(section)
        if value is not None and not isinstance(value, dict):
            return f"{section} must be an object, got {type(value).__name__}"

    receiver = cfg.get("receiver", {})
    for key in ("rx_antenna_gain_dbi", "sensitivity_dbm"):
        if key in receiver and not _is_number(receiver[key]):
            return f"receiver.{key} must be a number, got {receiver[key]!r}"

    for band, ranges in cfg.get("broadcast_bands", {}).items():
        if not isinstance(ranges, list):
            return f"broadcast_bands.{band} must be a list of [low, high] pairs"
        for r in ranges:
            if not isinstance(r, list) or len(r) != 2 or not all(_is_number(v) for v in r):
                return f"broadcast_bands.{band} entry must be a [low, high] pair of numbers, got {r!r}"
            if r[0] >= r[1]:
                return f"broadcast_bands.{band} range is not ascending: {r!r}"

    ranking = cfg.get("ranking", {})
    for key in ("band_priority", "distance_priority"):
        if key in ranking and not isinstance(ranking[key], dict):
            return f"ranking.{key} must be an object, got {type(ranking[key]).__name__}"

    classes = ranking.get("distance_classes")
    if classes is not None:
        if not isinstance(classes, list):
            return f"ranking.distance_classes must be a list, got {type(classes).__name__}"
        for i, dc in enumerate(classes):
            if not isinstance(dc, dict):
                return f"ranking.distance_classes[{i}] must be an object, got {type(dc).__name__}"
            # apply_config() indexes these three directly rather than .get()ing
            # them, which is what turned a wrong-shape PUT into a failed startup.
            for key in ("label", "min_km", "max_km"):
                if key not in dc:
                    return f"ranking.distance_classes[{i}] is missing {key}"
            if not isinstance(dc["label"], str) or not dc["label"]:
                return f"ranking.distance_classes[{i}].label must be a non-empty string"
            if not _is_number(dc["min_km"]):
                return f"ranking.distance_classes[{i}].min_km must be a number, got {dc['min_km']!r}"
            # max_km is nullable: the last class is open-ended.
            if dc["max_km"] is not None:
                if not _is_number(dc["max_km"]):
                    return f"ranking.distance_classes[{i}].max_km must be a number or null, got {dc['max_km']!r}"
                if dc["max_km"] <= dc["min_km"]:
                    return f"ranking.distance_classes[{i}] has max_km <= min_km"

    sort_order = ranking.get("sort_order")
    if sort_order is not None:
        if not isinstance(sort_order, list):
            return f"ranking.sort_order must be a list, got {type(sort_order).__name__}"
        for i, rule in enumerate(sort_order):
            if not isinstance(rule, dict) or "field" not in rule:
                return f"ranking.sort_order[{i}] must be an object with a field key"
            # The type check has to come first: an unhashable value such as a
            # list makes the membership test below raise, and a validator that
            # raises turns a 400 into a 500.
            if not isinstance(rule["field"], str):
                return f"ranking.sort_order[{i}].field must be a string, got {rule['field']!r}"
            # A field that is not sortable is not merely ignored: _sort_key()
            # negates a descending value, so naming a string field raises
            # TypeError on every search once this config is live.
            if rule["field"] not in _SORTABLE_FIELDS:
                allowed = ", ".join(sorted(_SORTABLE_FIELDS))
                return f"ranking.sort_order[{i}].field must be one of {allowed}, got {rule['field']!r}"
            if "ascending" in rule and not isinstance(rule["ascending"], bool):
                return f"ranking.sort_order[{i}].ascending must be true or false, got {rule['ascending']!r}"

    search = cfg.get("search", {})
    for key in ("default_radius_km", "default_limit"):
        if key in search and (not _is_number(search[key]) or search[key] <= 0):
            return f"search.{key} must be a positive number, got {search[key]!r}"

    return None


def apply_config(cfg: dict) -> None:
    """Push a config dict into the module-level settings.

    Raises on a shape this cannot handle. Every value is computed into a local
    first and the globals are assigned only once all of them exist, so a config
    that fails partway leaves the previous one intact rather than a half-applied
    mix of old and new that concurrent requests would serve.

    Callers that want a bad config to degrade rather than raise use
    reload_config(); PUT /api/config calls this directly, because it needs the
    failure in order to reject the write.
    """
    global RX_ANTENNA_GAIN_DBI, SENSITIVITY_DBM
    global BROADCAST_BANDS, BAND_PRIORITY
    global DISTANCE_CLASSES, DISTANCE_PRIORITY, SORT_ORDER
    global DEFAULT_RADIUS_KM, DEFAULT_LIMIT

    rx = cfg.get("receiver", {})
    rx_gain = rx.get("rx_antenna_gain_dbi", 6.0)
    sensitivity = rx.get("sensitivity_dbm", -95.0)

    bands = {band: [tuple(r) for r in ranges] for band, ranges in cfg.get("broadcast_bands", {}).items()}

    ranking = cfg.get("ranking", {})
    band_priority = ranking.get("band_priority", {"VHF": 0, "UHF": 1, "FM": 2})

    distance_classes = []
    for dc in ranking.get("distance_classes", []):
        max_km = dc["max_km"] if dc["max_km"] is not None else float("inf")
        distance_classes.append((dc["label"], dc["min_km"], max_km))

    distance_priority = ranking.get("distance_priority", {})
    sort_order = ranking.get(
        "sort_order",
        [
            {"field": "coverage_area_added_km2", "ascending": False},
            {"field": "band_priority", "ascending": True},
            {"field": "distance_priority", "ascending": True},
            {"field": "received_power_dbm", "ascending": False},
        ],
    )

    search = cfg.get("search", {})
    radius_km = search.get("default_radius_km", 80)
    limit = search.get("default_limit", 20)

    # Nothing above this line touches module state, and nothing below it can fail.
    RX_ANTENNA_GAIN_DBI = rx_gain
    SENSITIVITY_DBM = sensitivity
    BROADCAST_BANDS = bands
    BAND_PRIORITY = band_priority
    DISTANCE_CLASSES = distance_classes
    DISTANCE_PRIORITY = distance_priority
    SORT_ORDER = sort_order
    DEFAULT_RADIUS_KM = radius_km
    DEFAULT_LIMIT = limit


# Set when reload_config() could not use the on-disk overlay and fell back to
# defaults. compute_health_issues() reports it, so a degraded config shows up in
# /api/health and the alerter rather than only in one ERROR line at boot.
config_fallback_reason: str | None = None

# The shapes a config file can be wrong in: unreadable (OSError), not JSON or a
# bad value (ValueError), a section of the wrong type (TypeError/AttributeError),
# or missing a key apply_config indexes directly (KeyError).
_CONFIG_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)


def reload_config() -> None:
    """Re-read tower_config.json and update module-level settings.

    Fail-soft. This runs at import, and the overlay it reads is a persistent
    docker volume, so raising here turns one bad config into an outage that
    survives restart and redeploy, recoverable only by hand inside the volume.
    An unusable overlay is logged, recorded in config_fallback_reason for the
    health check, and replaced by the shipped defaults: a degraded ranking
    rather than a dead service.
    """
    global config_fallback_reason

    try:
        apply_config(_load_config())
        config_fallback_reason = None
        return
    except _CONFIG_ERRORS as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.error("tower_config: %s is unusable (%s), falling back to defaults", _CONFIG_PATH, reason)
    except Exception as exc:
        # Not a shape a config file can take, so this is a bug in apply_config
        # rather than a bad config. Still fail soft, because this runs at
        # import, but say which it is: the message above would send the reader
        # off to audit a config file that is perfectly fine.
        reason = f"unexpected {type(exc).__name__}: {exc}"
        logger.critical("tower_config: loader failed unexpectedly, this is a bug and not a bad config", exc_info=True)

    fallback = _default_config()
    if fallback is not None:
        try:
            apply_config(fallback)
            config_fallback_reason = reason
            return
        except _CONFIG_ERRORS:
            logger.error("tower_config: the shipped defaults are unusable too", exc_info=True)

    # Nothing usable anywhere: apply an empty config so every setting takes its
    # in-code default and the process can at least start.
    apply_config({})
    config_fallback_reason = reason


# Initialise on import
reload_config()


# Public names kept — the tower routes and their tests import both from here.
haversine = haversine_km
initial_bearing = bearing_deg


def bearing_to_cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
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
        try:
            inner = geom[geom.index("(") + 1 : geom.index(")")]
        except ValueError:
            # Malformed POINT WKT — missing opening or closing paren.
            return None
        parts = inner.split()
        if len(parts) >= 2:
            try:
                return float(parts[1]), float(parts[0])  # WKT is lng lat
            except ValueError:
                return None
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


def process_and_rank(
    raw_systems: list,
    user_lat: float,
    user_lon: float,
    limit: int = 0,
    user_frequencies: list[float] | None = None,
    radius_km: float = 0,
    *,
    coverage_scorer=None,
) -> list:
    """
    Takes raw system records from Maprad, filters and ranks them
    for passive radar suitability.

    Args:
        limit: Max towers to return. 0 means use DEFAULT_LIMIT from config.
        radius_km: Search radius in km. Towers beyond this are excluded.
                   0 means use DEFAULT_RADIUS_KM.
        coverage_scorer: Optional callable(deduped tower list) -> None,
                         annotating coverage_area_added_km2 (and friends) on
                         each tower in place. Run in a try/except: scoring must
                         never break the towers endpoint.
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

            towers.append(
                {
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
                }
            )

    # Deduplicate by (callsign, frequency) — keep the strongest
    seen = {}
    for t in towers:
        key = (t["callsign"], t["frequency_mhz"])
        if key not in seen or t["received_power_dbm"] > seen[key]["received_power_dbm"]:
            seen[key] = t
    towers = list(seen.values())

    if coverage_scorer is not None:
        try:
            coverage_scorer(towers)
        except Exception as exc:
            logging.warning("Coverage scoring failed: %s", exc)

    # Sort using configurable sort order
    # If user frequencies provided, frequency-matched towers sort first
    has_user_freqs = bool(user_frequencies)

    def _sort_key(t):
        parts = []
        # Frequency match is highest priority when user provided frequencies
        if has_user_freqs:
            parts.append(0 if t.get("frequency_matched") else 1)
        for rule in SORT_ORDER:
            # Fields are constrained to _SORTABLE_FIELDS on write, which is what
            # keeps the negation below from meeting a string.
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
