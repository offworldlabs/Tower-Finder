"""Tests for services/track_gates.py's coverage-calibration gate.

The detection area must be characterised by DETECTIONS, not by tracks that
merely still render.  A track keeps emitting for up to DISPLAY_STALE_TRACK_S
(15 s) after its last real detection with has_adsb still matching the live
ADS-B fix, so ungating calibration recording on that alone stamped positives
along an aircraft's exit path, far outside actual coverage, every emit cycle.
See config/constants.py's CAL_DETECTION_FRESH_S and the rationale duplicated
in track_gates.track_entry.
"""

import os
import time

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from config.constants import CAL_DETECTION_FRESH_S  # noqa: E402
from core import state  # noqa: E402
from services import track_gates  # noqa: E402

_NODE_CFG = {
    "node_id": "cal_node",
    "rx_lat": 33.939182,
    "rx_lon": -84.651910,
    "tx_lat": 33.756670,
    "tx_lon": -84.331844,
    "beam_width_deg": 90,
    "max_range_km": 100,
}

HEX = "cal1ab"


def _points(nid):
    ec = state.node_analytics.empirical_coverages.get(nid)
    return ec.n_points if ec else 0


def _furthest_count(nid):
    area = state.node_analytics.detection_areas.get(nid)
    return len(area.furthest_detections) if area else 0


@pytest.fixture()
def node():
    state.node_analytics.register_node(_NODE_CFG["node_id"], dict(_NODE_CFG))
    yield _NODE_CFG["node_id"]
    state.node_analytics.retire_node(_NODE_CFG["node_id"])


@pytest.fixture(autouse=True)
def _clean_state():
    state.active_geo_aircraft.clear()
    state.track_last_emit.clear()
    state.track_gate_hold.clear()
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    state.multinode_tracks.clear()
    state.track_histories.clear()
    state.accuracy_samples.clear()
    yield
    state.active_geo_aircraft.clear()
    state.track_last_emit.clear()
    state.track_gate_hold.clear()
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    state.multinode_tracks.clear()
    state.track_histories.clear()
    state.accuracy_samples.clear()


def _make_track(*, n_detections, last_detection_age_s, now):
    """A GeolocatedTrack-like object sitting inside the node's ADS-B fix.

    latest_delay_us=None keeps _cached_single_node_arc a no-op (no beam
    geometry to satisfy), which isolates this test to the calibration gate
    rather than the arc-building path.
    """
    from pipeline.passive_radar import GeolocatedTrack
    track = GeolocatedTrack(
        track_id=f"track-{HEX}",
        lat=33.90, lon=-84.60, alt_m=3000,
        vel_east=0.0, vel_north=0.0, vel_up=0.0,
        rms_delay=1.0, rms_doppler=1.0, n_detections=n_detections,
        timestamp_ms=int(now * 1000),
        adsb_hex=HEX, latest_delay_us=None, target_class="aircraft",
    )
    track.wall_clock_ts = now
    track.last_detection_wall_ts = now - last_detection_age_s
    return track


def _adsb_fix(now, *, lat=33.90, lon=-84.60, age_s=1.0):
    state.adsb_aircraft[HEX] = {
        "lat": lat, "lon": lon,
        "gs": 400.0, "track": 90.0,
        "alt_baro": 30000,
        "last_seen_ms": (now - age_s) * 1000.0,
    }


class TestFreshDetectionRecordsCalibration:
    def test_fresh_stamp_and_n_detections_records_calibration_and_verified_detection(self, node):
        now = time.time()
        _adsb_fix(now)
        track = _make_track(n_detections=3, last_detection_age_s=1.0, now=now)
        entry = track_gates.track_entry(HEX, track, dict(_NODE_CFG), now, set())

        assert entry is not None
        assert _points(node) == 1
        assert _furthest_count(node) == 1
        # Accuracy is always recorded when ADS-B is live, regardless of the
        # detection-freshness gate -- honest even while coasting.
        assert len(state.accuracy_samples) == 1

    def test_stamp_aged_past_the_budget_records_neither_but_still_emits_and_still_scores_accuracy(self, node):
        now = time.time()
        _adsb_fix(now)
        track = _make_track(
            n_detections=3,
            last_detection_age_s=CAL_DETECTION_FRESH_S + 1.0,
            now=now,
        )
        entry = track_gates.track_entry(HEX, track, dict(_NODE_CFG), now, set())

        assert entry is not None  # display path is unaffected
        assert _points(node) == 0
        assert _furthest_count(node) == 0
        assert len(state.accuracy_samples) == 1

    def test_stamp_exactly_at_the_budget_still_counts(self, node):
        now = time.time()
        _adsb_fix(now)
        track = _make_track(
            n_detections=3,
            last_detection_age_s=CAL_DETECTION_FRESH_S,
            now=now,
        )
        track_gates.track_entry(HEX, track, dict(_NODE_CFG), now, set())
        assert _points(node) == 1

    def test_fewer_than_three_detections_records_neither(self, node):
        now = time.time()
        _adsb_fix(now)
        track = _make_track(n_detections=2, last_detection_age_s=0.5, now=now)
        entry = track_gates.track_entry(HEX, track, dict(_NODE_CFG), now, set())

        assert entry is not None
        assert _points(node) == 0
        assert _furthest_count(node) == 0
        # Accuracy is unaffected by the n_detections floor -- it's a
        # coverage-only gate.
        assert len(state.accuracy_samples) == 1

    def test_missing_last_detection_wall_ts_treated_as_never_fresh(self, node):
        """getattr default of 0.0 -- a track type that never sets the
        attribute (e.g. an old/foreign stub) must not accidentally pass the
        freshness check."""
        now = time.time()
        _adsb_fix(now)
        track = _make_track(n_detections=5, last_detection_age_s=0.0, now=now)
        del track.last_detection_wall_ts
        track_gates.track_entry(HEX, track, dict(_NODE_CFG), now, set())
        assert _points(node) == 0
