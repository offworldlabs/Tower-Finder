"""Unit tests for the _fresh_adsb closure inside build_combined_aircraft_json.

Since _fresh_adsb is a nested closure it cannot be imported directly.
Tests drive it via build_combined_aircraft_json(pipeline) and inspect the
resulting aircraft list.
"""

import time
import types

import pytest

from core import state
from pipeline.passive_radar import GeolocatedTrack
from services.frame_processor import build_combined_aircraft_json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX = "aa1234"


def _make_pipeline():
    return types.SimpleNamespace(
        geolocated_tracks={},
        config={"node_id": "test-node"},
    )


def _make_track(alt_m=3000.0):
    return GeolocatedTrack(
        track_id="t1",
        lat=48.0,
        lon=16.0,
        alt_m=alt_m,
        vel_east=50.0,
        vel_north=100.0,
        vel_up=0.0,
        rms_delay=0.0,
        rms_doppler=0.0,
        n_detections=5,
        timestamp_ms=0,
        adsb_hex=_HEX,
    )


def _seed_active_geo(track=None):
    t = track or _make_track()
    state.active_geo_aircraft[_HEX] = (t, {"node_id": "test-node"})
    return t


def _run(pipeline=None):
    p = pipeline or _make_pipeline()
    result = build_combined_aircraft_json(p)
    return next((a for a in result["aircraft"] if a["hex"] == _HEX), None)


def _base_ext(alt_m=3048.0, velocity=257.222, heading=45.0):
    return {
        "lat": 48.0,
        "lon": 16.0,
        "alt_m": alt_m,
        "velocity": velocity,
        "heading": heading,
    }


# ---------------------------------------------------------------------------
# Fixture: clean state before and after every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_state():
    state.active_geo_aircraft.clear()
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    state.multinode_tracks.clear()
    state.track_histories.clear()
    state.ground_truth_trails.clear()
    state.ground_truth_meta.clear()
    yield
    state.active_geo_aircraft.clear()
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    state.multinode_tracks.clear()
    state.track_histories.clear()
    state.ground_truth_trails.clear()
    state.ground_truth_meta.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFreshAdsbFallback:

    def test_live_feed_returned_when_fresh(self):
        """Fresh live ADS-B entry wins over external cache."""
        _seed_active_geo()
        # Fresh live entry: last_seen_ms = now - 1 s
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": 48.0,
            "lon": 16.0,
            "alt_baro": 30000,
            "gs": 400.0,
            "track": 90.0,
            "last_seen_ms": int(time.time() * 1000) - 1000,
        }
        # External cache would yield ~3281 ft, not 30000
        state.external_adsb_cache[_HEX] = _base_ext(alt_m=1000.0)

        ac = _run()
        assert ac is not None, "aircraft not found in result"
        assert ac["alt_baro"] == 30000

    def test_external_cache_used_when_live_stale(self):
        """Stale live entry (last_seen_ms=0) falls back to external cache."""
        _seed_active_geo()
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": 48.0,
            "lon": 16.0,
            "alt_baro": 99999,
            "gs": 0.0,
            "track": 0.0,
            "last_seen_ms": 0,   # age >> 60 s → stale
        }
        state.external_adsb_cache[_HEX] = _base_ext(alt_m=3048.0)

        ac = _run()
        assert ac is not None
        assert ac["alt_baro"] == 10000  # 3048 m / 0.3048 = 10000 ft

    def test_external_cache_used_when_live_absent(self):
        """No live entry at all → external cache is used with unit conversion."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(alt_m=3048.0)

        ac = _run()
        assert ac is not None
        assert ac["alt_baro"] == 10000

    def test_altitude_m_to_ft_conversion(self):
        """1524 m → 5000 ft."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(alt_m=1524.0)

        ac = _run()
        assert ac is not None
        assert ac["alt_baro"] == round(1524.0 / 0.3048)  # 5000

    def test_velocity_ms_to_knots_conversion(self):
        """51.4444 m/s → 100.0 knots."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(velocity=51.4444)

        ac = _run()
        assert ac is not None
        assert ac["gs"] == round(51.4444 * 1.94384, 1)  # 100.0

    def test_heading_passthrough(self):
        """Heading is passed through without conversion."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(heading=270.0)

        ac = _run()
        assert ac is not None
        assert ac["track"] == 270.0

    def test_non_numeric_alt_defaults_to_zero(self):
        """Non-numeric alt_m → alt_baro == 0."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(alt_m="invalid")

        ac = _run()
        assert ac is not None
        assert ac["alt_baro"] == 0

    def test_non_numeric_velocity_defaults_to_zero(self):
        """None velocity → gs == 0.0."""
        _seed_active_geo()
        ext = _base_ext()
        ext["velocity"] = None
        state.external_adsb_cache[_HEX] = ext

        ac = _run()
        assert ac is not None
        assert ac["gs"] == 0.0

    def test_missing_lat_returns_no_aircraft(self):
        """No lat in external cache → _fresh_adsb returns None → solver fallback used."""
        # track alt_m=3000.0 → alt_ft ≈ 9843
        _seed_active_geo(_make_track(alt_m=3000.0))
        # external cache has no lat → _fresh_adsb returns None
        state.external_adsb_cache[_HEX] = {"lon": 16.0, "alt_m": 1000.0}

        ac = _run()
        assert ac is not None
        # External cache alt_m=1000.0 would give ≈3281 ft; solver gives ≈9843 ft.
        assert ac["alt_baro"] != round(1000.0 / 0.3048)

    def test_zero_alt_m_produces_zero_alt_baro(self):
        """alt_m=0 → alt_baro == 0."""
        _seed_active_geo()
        state.external_adsb_cache[_HEX] = _base_ext(alt_m=0)

        ac = _run()
        assert ac is not None
        assert ac["alt_baro"] == 0
