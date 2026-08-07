"""Feed-side handling of multinode solver tracks.

Covers the dead-reckoning of mn-* entries in build_combined_aircraft_json:
position is advanced with the solved velocity, but only up to a 30 s horizon —
past that a velocity error dominates any solve accuracy, so an old solve holds
its last dead-reckoned point until the 60 s entry expiry.
"""

import os
import time
import types

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402

LAT, LON = 35.0, -82.0


def _mn_entry(age_s: float, vel_north: float) -> dict:
    return {
        "success": True,
        "lat": LAT,
        "lon": LON,
        "alt_m": 7000.0,
        "vel_east": 0.0,
        "vel_north": vel_north,
        "rms_delay": 0.1,
        "rms_doppler": 1.0,
        "n_nodes": 2,
        "n_measurements": 2,
        "contributing_node_ids": ["n1", "n2"],
        "timestamp_ms": int((time.time() - age_s) * 1000),
    }


class TestMultinodeDeadReckonCap:
    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def test_fresh_entry_is_dead_reckoned_fully(self):
        state.multinode_tracks["mn-dark-x"] = _mn_entry(age_s=10.0, vel_north=100.0)
        ac = self._build_mn()
        exp_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 10.0)
        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)

    def test_dr_horizon_is_capped_at_30s(self):
        # 45 s old (younger than the 60 s expiry): advanced 30 s worth of
        # motion, not 45.
        state.multinode_tracks["mn-dark-x"] = _mn_entry(age_s=45.0, vel_north=100.0)
        ac = self._build_mn()
        capped_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 30.0)
        uncapped_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 45.0)
        assert ac["lat"] == pytest.approx(capped_lat, abs=2e-4)
        assert abs(ac["lat"] - uncapped_lat) > 5e-3
