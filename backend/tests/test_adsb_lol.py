"""
Unit tests for the adsb.lol client and metro filtering logic.
"""

import json
import time
from unittest.mock import patch, MagicMock

import pytest

from clients.adsb_lol import AdsbLolClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

_AREAS = [
    {"name": "Atlanta", "lat": 33.749, "lon": -84.388, "radius_nm": 80},
    {"name": "Greenville", "lat": 34.852, "lon": -82.394, "radius_nm": 60},
]

_MOCK_RESPONSE = {
    "ac": [
        {"hex": "a1b2c3", "flight": "DAL123 ", "lat": 33.75, "lon": -84.39,
         "alt_baro": 35000, "gs": 450, "track": 90, "squawk": "1234",
         "category": "A3", "type": "adsb_icao", "r": "N12345", "t": "B738"},
        {"hex": "d4e5f6", "lat": 33.80, "lon": -84.30, "alt_baro": 28000,
         "gs": 380, "track": 180},
        # Aircraft with no lat/lon should be skipped
        {"hex": "no_pos", "alt_baro": 10000},
    ],
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAdsbLolClient:
    def _mock_urlopen(self, response_data):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_returns_aircraft(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        result = client.fetch_area(_AREAS[0])

        assert len(result) == 2  # no_pos aircraft skipped
        assert result[0]["hex"] == "a1b2c3"
        assert result[0]["flight"] == "DAL123"  # stripped
        assert result[0]["lat"] == 33.75
        assert result[0]["registration"] == "N12345"
        assert result[1]["hex"] == "d4e5f6"

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_caches_within_interval(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)

        first = client.fetch_area(_AREAS[0])
        assert len(first) == 2
        assert mock_urlopen.call_count == 1

        # Second call within interval should use cache
        second = client.fetch_area(_AREAS[0])
        assert len(second) == 2
        assert mock_urlopen.call_count == 1  # no new request

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_returns_cache_on_error(self, mock_urlopen):
        # First call succeeds
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])

        # Expire cache
        client._last_poll["Atlanta"] = 0

        # Second call fails
        mock_urlopen.side_effect = Exception("network error")
        result = client.fetch_area(_AREAS[0])
        assert len(result) == 2  # returns cached data

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_all_deduplicates(self, mock_urlopen):
        # Same aircraft in both areas
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        result = client.fetch_all()

        # Should deduplicate by hex
        hexes = [ac["hex"] for ac in result]
        assert len(hexes) == len(set(hexes))

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_url_format(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen({"ac": []})
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "33.749" in req.full_url
        assert "-84.388" in req.full_url
        assert "/80" in req.full_url

    def test_empty_areas(self):
        client = AdsbLolClient([])
        assert client.fetch_all() == []
