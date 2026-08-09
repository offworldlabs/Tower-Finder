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
from services import track_filter  # noqa: E402
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
        # Confirmed (solve_count >= 2) so the MN_N2_MIN_SOLVES display gate
        # does not hide these fixtures — this file tests dead-reckoning, not
        # the gate itself; see test_mn_lifetime.py for the gate tests.
        "solve_count": 2,
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


class TestMultinodeDeadReckonSource:
    """TRACK_DR_SOURCE selects what velocity the DR block above uses: the KF
    display filter's LEARNED velocity (default, "kf") or the solved
    vel_east/vel_north the block used exclusively before this existed
    ("solve", rollback only).  This class's keys always seed a real KF entry
    first -- TestMultinodeDeadReckonCap's keys never touch track_filter at
    all, which is exactly what proves the "no KF entry" fallback path (the
    default env, unknown key) is unaffected by this feature.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def _seed_kf_entry(self, key: str, v_north_true: float) -> None:
        """Two solves, 10 s apart, moving purely north at v_north_true --
        vel_east/vel_north left at 0.0 on the fed results so the filter's
        learned velocity comes from the POSITION sequence, not an echoed
        prior (same discipline as test_track_filter.TestLearnedVelocity)."""
        lat2, lon2 = offset_latlon_m(LAT, LON, east_m=0.0, north_m=v_north_true * 10.0)
        track_filter.smooth_solve(
            {"lat": LAT, "lon": LON, "timestamp_ms": 1_000,
             "vel_east": 0.0, "vel_north": 0.0}, key, None)
        track_filter.smooth_solve(
            {"lat": lat2, "lon": lon2, "timestamp_ms": 21_000,
             "vel_east": 0.0, "vel_north": 0.0}, key, None)

    def test_kf_entry_dr_uses_learned_velocity_by_default(self, monkeypatch):
        monkeypatch.delenv("TRACK_DR_SOURCE", raising=False)
        key = "mn-kf-learn"
        self._seed_kf_entry(key, v_north_true=50.0)
        lv = track_filter.learned_velocity(key)
        assert lv is not None
        learned_ve, learned_vn = lv[0], lv[1]

        # The solve's OWN vel_north (100.0) differs from the learned one
        # (~50.0) -- the point of the test: prove the DR block reads the KF,
        # not the solve, whenever a KF entry actually exists.
        age_s = 10.0
        state.multinode_tracks[key] = _mn_entry(age_s=age_s, vel_north=100.0)
        ac = self._build_mn()

        exp_lat, exp_lon = offset_latlon_m(
            LAT, LON, east_m=learned_ve * age_s, north_m=learned_vn * age_s)
        solve_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * age_s)

        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)
        assert ac["lon"] == pytest.approx(exp_lon, abs=2e-4)
        assert abs(ac["lat"] - solve_lat) > 1e-4

    def test_track_dr_source_solve_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setenv("TRACK_DR_SOURCE", "solve")
        key = "mn-kf-rollback"
        self._seed_kf_entry(key, v_north_true=50.0)
        assert track_filter.learned_velocity(key) is not None   # KF entry genuinely exists

        age_s = 10.0
        state.multinode_tracks[key] = _mn_entry(age_s=age_s, vel_north=100.0)
        ac = self._build_mn()

        # "solve" must ignore the (present, different) KF entry entirely and
        # extrapolate with the solve's own vel_north, exactly as before this
        # feature existed.
        exp_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * age_s)
        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)
