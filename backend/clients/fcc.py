"""
FCC broadcast station client for US tower data.

Queries the FCC TV Query and FM Query CGI endpoints to get broadcast
station data directly from the authoritative FCC LMS database.
This supplements Maprad.io data and ensures complete US coverage.

TV Query: https://transition.fcc.gov/cgi-bin/tvq
FM Query: https://transition.fcc.gov/cgi-bin/fmq
"""

import logging
import math
import re

import httpx

log = logging.getLogger(__name__)

_TV_URL = "https://transition.fcc.gov/cgi-bin/tvq"
_FM_URL = "https://transition.fcc.gov/cgi-bin/fmq"

# Channel → approximate center frequency (MHz) for US TV channels
# Channels 2-6 (VHF-Lo), 7-13 (VHF-Hi), 14-36 (UHF)
_TV_CHANNEL_FREQ = {}
# VHF Low: 54-88 MHz (channels 2-6)
for _ch, _f in [(2, 57), (3, 63), (4, 69), (5, 79), (6, 85)]:
    _TV_CHANNEL_FREQ[_ch] = _f
# VHF High: 174-216 MHz (channels 7-13)
for _ch in range(7, 14):
    _TV_CHANNEL_FREQ[_ch] = 174 + (_ch - 7) * 6 + 3
# UHF: 470-608 MHz (channels 14-36)
for _ch in range(14, 37):
    _TV_CHANNEL_FREQ[_ch] = 470 + (_ch - 14) * 6 + 3


def _parse_erp_kw(erp_str: str) -> float | None:
    """Parse ERP string like '180.   kW' or '0.01   kW' to float kW."""
    if not erp_str:
        return None
    m = re.search(r'([\d.]+)\s*kW', erp_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _erp_kw_to_eirp_dbm(erp_kw: float) -> float:
    """Convert ERP in kW to approximate EIRP in dBm.

    EIRP ≈ ERP + 2.15 dB (dipole-to-isotropic correction).
    """
    if erp_kw <= 0:
        return float("-inf")
    erp_dbm = 10 * math.log10(erp_kw * 1000) + 30
    return erp_dbm + 2.15


def _parse_tv_line(line: str) -> dict | None:
    """Parse a pipe-delimited TV Query result line into a device dict.

    Returns a dict compatible with Maprad device format for process_and_rank().
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 35:
        return None

    try:
        callsign = parts[1]
        service_type = parts[3]  # DTV, LPT, LPD, DTS, etc.
        channel_str = parts[4]
        status = parts[9]  # LIC, CP, MOD, etc.

        # Only include licensed stations
        if status not in ("LIC",):
            return None

        # Parse channel → frequency
        try:
            channel = int(channel_str)
        except ValueError:
            return None
        freq_mhz = _TV_CHANNEL_FREQ.get(channel)
        if freq_mhz is None:
            return None

        # Parse ERP
        erp_str = parts[14]
        erp_kw = _parse_erp_kw(erp_str)

        # Parse coordinates (DMS)
        lat_ns = parts[19]
        lat_d = int(parts[20])
        lat_m = int(parts[21])
        lat_s = float(parts[22])
        lon_ew = parts[23]
        lon_d = int(parts[24])
        lon_m = int(parts[25])
        lon_s = float(parts[26])

        lat = lat_d + lat_m / 60 + lat_s / 3600
        if lat_ns == "S":
            lat = -lat
        lon = lon_d + lon_m / 60 + lon_s / 3600
        if lon_ew == "W":
            lon = -lon

        # Parse antenna height
        antenna_height_str = parts[16]  # HAAT in meters
        antenna_height = None
        try:
            antenna_height = float(antenna_height_str)
        except (ValueError, TypeError):
            pass

        # Build Maprad-compatible device dict
        eirp_watts = None
        if erp_kw is not None and erp_kw > 0:
            eirp_dbm = _erp_kw_to_eirp_dbm(erp_kw)
            eirp_watts = 10 ** ((eirp_dbm - 30) / 10)

        city = parts[10]
        state = parts[11]

        return {
            "callsign": callsign,
            "frequency": freq_mhz,
            "eirp": eirp_watts,
            "transmitPower": None,
            "antennaHeight": antenna_height,
            "location": {
                "name": f"{city.strip().title()}, {state.strip()}",
                "state": state.strip(),
                "geom": {"string": f"POINT({lon} {lat})"},
            },
            "_fcc_service_type": service_type,
            "_fcc_channel": channel,
        }
    except (ValueError, IndexError) as exc:
        log.debug("Failed to parse TV line: %s", exc)
        return None


def _parse_fm_line(line: str) -> dict | None:
    """Parse a pipe-delimited FM Query result line into a device dict."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 35:
        return None

    try:
        callsign = parts[1]
        freq_str = parts[2]  # e.g. "89.3  MHz"
        service_type = parts[3]  # FM, FS (booster), FX (translator)
        status = parts[9]

        if status not in ("LIC",):
            return None

        # Parse frequency
        freq_match = re.search(r'([\d.]+)\s*MHz', freq_str)
        if not freq_match:
            return None
        freq_mhz = float(freq_match.group(1))

        # Parse ERP (two fields: horizontal, vertical)
        erp_h_str = parts[14]
        erp_kw = _parse_erp_kw(erp_h_str)

        # Parse coordinates
        lat_ns = parts[19]
        lat_d = int(parts[20])
        lat_m = int(parts[21])
        lat_s = float(parts[22])
        lon_ew = parts[23]
        lon_d = int(parts[24])
        lon_m = int(parts[25])
        lon_s = float(parts[26])

        lat = lat_d + lat_m / 60 + lat_s / 3600
        if lat_ns == "S":
            lat = -lat
        lon = lon_d + lon_m / 60 + lon_s / 3600
        if lon_ew == "W":
            lon = -lon

        # Parse antenna height (HAAT)
        antenna_height_str = parts[16]
        antenna_height = None
        try:
            antenna_height = float(antenna_height_str)
        except (ValueError, TypeError):
            pass

        eirp_watts = None
        if erp_kw is not None and erp_kw > 0:
            eirp_dbm = _erp_kw_to_eirp_dbm(erp_kw)
            eirp_watts = 10 ** ((eirp_dbm - 30) / 10)

        city = parts[10]
        state = parts[11]

        return {
            "callsign": callsign,
            "frequency": freq_mhz,
            "eirp": eirp_watts,
            "transmitPower": None,
            "antennaHeight": antenna_height,
            "location": {
                "name": f"{city.strip().title()}, {state.strip()}",
                "state": state.strip(),
                "geom": {"string": f"POINT({lon} {lat})"},
            },
            "_fcc_service_type": service_type,
        }
    except (ValueError, IndexError) as exc:
        log.debug("Failed to parse FM line: %s", exc)
        return None


def _nearby_states(lat: float, lon: float) -> list[str]:
    """Return US state codes near a coordinate.

    Queries the FCC for the primary state plus neighbouring states
    to ensure we capture towers that may be across state lines but
    within the search radius.
    """
    # State centroids (approximate) for distance-based selection
    _STATES = {
        "AL": (32.8, -86.8), "AK": (64.2, -152.5), "AZ": (34.0, -111.1),
        "AR": (35.2, -91.8), "CA": (36.8, -119.4), "CO": (39.5, -105.8),
        "CT": (41.6, -72.7), "DE": (38.9, -75.5), "FL": (27.8, -81.8),
        "GA": (32.2, -83.6), "HI": (19.9, -155.6), "ID": (44.1, -114.7),
        "IL": (40.3, -89.0), "IN": (40.3, -86.1), "IA": (42.0, -93.2),
        "KS": (38.5, -98.8), "KY": (37.8, -84.3), "LA": (30.5, -92.0),
        "ME": (45.3, -69.4), "MD": (39.0, -76.6), "MA": (42.4, -71.4),
        "MI": (44.3, -85.6), "MN": (46.4, -94.6), "MS": (32.7, -89.7),
        "MO": (38.5, -92.3), "MT": (46.8, -110.4), "NE": (41.1, -98.3),
        "NV": (38.8, -116.4), "NH": (43.2, -71.6), "NJ": (40.1, -74.4),
        "NM": (34.8, -106.2), "NY": (43.0, -75.0), "NC": (35.6, -79.0),
        "ND": (47.5, -100.5), "OH": (40.4, -82.9), "OK": (35.0, -97.1),
        "OR": (43.8, -120.6), "PA": (41.2, -77.2), "RI": (41.6, -71.5),
        "SC": (34.0, -81.0), "SD": (43.9, -99.4), "TN": (35.5, -86.6),
        "TX": (31.1, -99.7), "UT": (39.3, -111.1), "VT": (44.0, -72.7),
        "VA": (37.4, -78.2), "WA": (47.4, -120.7), "WV": (38.6, -80.6),
        "WI": (43.8, -89.5), "WY": (43.1, -107.6), "DC": (38.9, -77.0),
        "PR": (18.2, -66.6), "VI": (18.3, -64.9), "GU": (13.4, 144.8),
        "AS": (-14.3, -170.7), "MP": (15.2, 145.7),
    }

    # Sort states by distance from the search point, take closest N
    dists = []
    for code, (slat, slon) in _STATES.items():
        d = math.sqrt((lat - slat) ** 2 + (lon - slon) ** 2)
        dists.append((d, code))
    dists.sort()

    # Take the closest state plus neighbours within ~3 degrees
    primary = dists[0][1]
    result = [primary]
    for d, code in dists[1:]:
        if d < 4.0:  # ~4 degrees ≈ 350-440 km, covers 80km radius well
            result.append(code)
        else:
            break
    return result[:5]  # Cap at 5 states to avoid excessive queries


async def fetch_fcc_tv_stations(
    lat: float,
    lon: float,
    radius_km: int = 80,
    states: list[str] | None = None,
) -> list[dict]:
    """Fetch TV broadcast stations from FCC TV Query.

    Args:
        lat: Search center latitude
        lon: Search center longitude
        radius_km: Search radius (used for state selection if states not given)
        states: Explicit list of state codes to query

    Returns:
        List of Maprad-compatible system dicts with devices.
    """
    if states is None:
        states = _nearby_states(lat, lon)

    all_devices = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for state in states:
            try:
                params = {
                    "list": "4",
                    "state": state,
                    "city": "",
                    "chan": "0",
                    "type": "4",  # All service types
                    "status": "3",  # Licensed only
                }
                resp = await client.get(
                    _TV_URL,
                    params=params,
                    headers={"User-Agent": "TowerFinder/1.0"},
                )
                resp.raise_for_status()
                text = resp.text

                for line in text.strip().split("\n"):
                    if not line.startswith("|"):
                        continue
                    device = _parse_tv_line(line)
                    if device is not None:
                        all_devices.append(device)

            except Exception as exc:
                log.warning("FCC TV query for state %s failed: %s", state, exc)

    # Wrap devices as Maprad-compatible system dicts
    systems = []
    for dev in all_devices:
        systems.append({
            "id": f"fcc-tv-{dev['callsign']}-{dev.get('_fcc_channel', '')}",
            "devices": [dev],
            "licence": {"type": "Broadcasting", "subtype": "Television"},
        })

    log.info("FCC TV: fetched %d stations from states %s", len(systems), states)
    return systems


async def fetch_fcc_fm_stations(
    lat: float,
    lon: float,
    radius_km: int = 80,
    states: list[str] | None = None,
) -> list[dict]:
    """Fetch FM broadcast stations from FCC FM Query.

    Args:
        lat: Search center latitude
        lon: Search center longitude
        radius_km: Search radius (used for state selection if states not given)
        states: Explicit list of state codes to query

    Returns:
        List of Maprad-compatible system dicts with devices.
    """
    if states is None:
        states = _nearby_states(lat, lon)

    all_devices = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for state in states:
            try:
                params = {
                    "list": "4",
                    "state": state,
                    "city": "",
                    "type": "4",
                    "status": "3",
                }
                resp = await client.get(
                    _FM_URL,
                    params=params,
                    headers={"User-Agent": "TowerFinder/1.0"},
                )
                resp.raise_for_status()
                text = resp.text

                for line in text.strip().split("\n"):
                    if not line.startswith("|"):
                        continue
                    device = _parse_fm_line(line)
                    if device is not None:
                        all_devices.append(device)

            except Exception as exc:
                log.warning("FCC FM query for state %s failed: %s", state, exc)

    systems = []
    for dev in all_devices:
        systems.append({
            "id": f"fcc-fm-{dev['callsign']}-{dev.get('frequency', '')}",
            "devices": [dev],
            "licence": {"type": "Broadcasting", "subtype": "FM Radio"},
        })

    log.info("FCC FM: fetched %d stations from states %s", len(systems), states)
    return systems


async def fetch_fcc_broadcast_systems(
    lat: float,
    lon: float,
    radius_km: int = 80,
) -> list[dict]:
    """Fetch all broadcast systems (TV + FM) from FCC for a location.

    Returns Maprad-compatible system dicts that can be passed directly
    to calculations.process_and_rank().
    """
    import asyncio

    tv_task = fetch_fcc_tv_stations(lat, lon, radius_km)
    fm_task = fetch_fcc_fm_stations(lat, lon, radius_km)

    tv_systems, fm_systems = await asyncio.gather(tv_task, fm_task)
    return tv_systems + fm_systems
