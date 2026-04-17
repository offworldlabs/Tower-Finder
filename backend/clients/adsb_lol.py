"""
adsb.lol API client — fetches real-time ADS-B aircraft positions.

API: https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}
Returns tar1090-compatible aircraft objects.
License: ODbL (same as OpenStreetMap).
"""

import json
import logging
import time
import urllib.request

log = logging.getLogger(__name__)

_BASE = "https://api.adsb.lol/v2"
_TIMEOUT = 8  # seconds
_MIN_POLL_INTERVAL = 5.0  # seconds between requests per area


class AdsbLolClient:
    """Polls adsb.lol for real aircraft in configured geographic areas."""

    def __init__(self, areas: list[dict]):
        """
        Args:
            areas: List of dicts with keys: name, lat, lon, radius_nm (default 80).
        """
        self.areas = areas
        self._last_poll: dict[str, float] = {}
        self._cache: dict[str, list[dict]] = {}

    def fetch_area(self, area: dict) -> list[dict]:
        """Fetch aircraft for a single area. Returns list of aircraft dicts."""
        name = area["name"]
        now = time.monotonic()
        if now - self._last_poll.get(name, 0) < _MIN_POLL_INTERVAL:
            return self._cache.get(name, [])

        lat = area["lat"]
        lon = area["lon"]
        radius_nm = area.get("radius_nm", 80)
        url = f"{_BASE}/point/{lat}/{lon}/{radius_nm}"

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
            aircraft = data.get("ac", [])
            result = []
            for ac in aircraft:
                lat_v = ac.get("lat")
                lon_v = ac.get("lon")
                if lat_v is None or lon_v is None:
                    continue
                result.append({
                    "hex": ac.get("hex", ""),
                    "flight": (ac.get("flight") or "").strip(),
                    "lat": lat_v,
                    "lon": lon_v,
                    "alt_baro": ac.get("alt_baro") or 0,
                    "gs": ac.get("gs") or 0,
                    "track": ac.get("track") or 0,
                    "squawk": ac.get("squawk", ""),
                    "category": ac.get("category", ""),
                    "type": ac.get("type", "adsb_icao"),
                    "registration": ac.get("r", ""),
                    "aircraft_type": ac.get("t", ""),
                })
            self._cache[name] = result
            self._last_poll[name] = now
            return result
        except Exception as e:
            log.debug("adsb.lol fetch failed for %s: %s", name, e)
            return self._cache.get(name, [])

    def fetch_all(self) -> list[dict]:
        """Fetch aircraft for all configured areas, deduplicated by hex."""
        seen = set()
        result = []
        for area in self.areas:
            for ac in self.fetch_area(area):
                h = ac.get("hex", "")
                if h and h not in seen:
                    seen.add(h)
                    result.append(ac)
        return result
