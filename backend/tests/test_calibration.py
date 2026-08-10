"""One age rule for coverage calibration points.

The frame path and the solve path recorded these independently and disagreed on
the only parameter that matters: the solver gated at 10 s, the frame path used
_fresh_adsb's 60 s display-freshness window and applied no calibration gate at
all.  At 250 m/s that is 15 km of travel recorded against a 5-degree polar grid
whose entire purpose is to say where a node can see.

A second, independent rule lives here too: fix_ts/detection_ts and the
CAL_FIX_DETECTION_SKEW_S gate (TestFixDetectionSkewGate below).  age_s bounds
the fix against `now`; the skew gate bounds the fix against the DETECTION
EVENT it is meant to describe — a fix can be fresh by both age_s and
CAL_DETECTION_FRESH_S and still be an exit smear, the live position recorded
for a few seconds after the node actually lost the target.  See the module
docstring in services/calibration.py.
"""

import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from config.constants import CAL_FIX_DETECTION_SKEW_S, CAL_MAX_ADSB_AGE_S  # noqa: E402
from core import state  # noqa: E402
from services.calibration import record_adsb_calibration  # noqa: E402

_CFG = dict(rx_lat=34.85, rx_lon=-82.40, tx_lat=34.90, tx_lon=-82.30,
            max_range_km=50, max_bistatic_range_km=60)

# A matching fix_ts/detection_ts pair (zero skew) for tests that are not
# themselves about the skew gate — the age-gate and position-guard tests
# below predate fix_ts/detection_ts and must not incidentally exercise it.
_T0 = 1_000_000.0


@pytest.fixture()
def nodes():
    for nid in ("cal-a", "cal-b"):
        state.node_analytics.register_node(nid, dict(_CFG))
    yield ("cal-a", "cal-b")
    for nid in ("cal-a", "cal-b"):
        state.node_analytics.retire_node(nid)


def _points(nid):
    ec = state.node_analytics.empirical_coverages.get(nid)
    return ec.n_points if ec else 0


class TestAgeGate:
    def test_a_fresh_fix_is_recorded(self, nodes):
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0,
                                       fix_ts=_T0, detection_ts=_T0) == 2
        assert _points("cal-a") == 1

    def test_a_fix_at_the_limit_is_recorded(self, nodes):
        assert record_adsb_calibration(nodes, 34.9, -82.35,
                                       age_s=CAL_MAX_ADSB_AGE_S,
                                       fix_ts=_T0, detection_ts=_T0) == 2

    def test_a_stale_fix_is_refused(self, nodes):
        """60 s was what the frame path accepted — 15 km at cruise."""
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=60.0,
                                       fix_ts=_T0, detection_ts=_T0) == 0
        assert _points("cal-a") == 0

    def test_the_limit_is_the_solvers_stated_one(self):
        assert CAL_MAX_ADSB_AGE_S == 10.0


class TestPositionGuard:
    def test_a_missing_position_is_refused(self, nodes):
        assert record_adsb_calibration(nodes, None, -82.35, age_s=1.0,
                                       fix_ts=_T0, detection_ts=_T0) == 0
        assert record_adsb_calibration(nodes, 34.9, None, age_s=1.0,
                                       fix_ts=_T0, detection_ts=_T0) == 0

    def test_null_island_is_refused(self, nodes):
        """0,0 is what an absent field parses to, not a position in the Gulf of
        Guinea."""
        assert record_adsb_calibration(nodes, 0, 0, age_s=1.0,
                                       fix_ts=_T0, detection_ts=_T0) == 0

    def test_blank_node_ids_are_skipped(self, nodes):
        assert record_adsb_calibration([None, "", "cal-a"], 34.9, -82.35,
                                       age_s=1.0, fix_ts=_T0, detection_ts=_T0) == 1


class TestFanOut:
    def test_every_contributing_node_gets_the_point(self, nodes):
        record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0,
                                fix_ts=_T0, detection_ts=_T0)
        assert _points("cal-a") == 1
        assert _points("cal-b") == 1

    def test_an_empty_node_list_records_nothing(self, nodes):
        assert record_adsb_calibration([], 34.9, -82.35, age_s=1.0,
                                       fix_ts=_T0, detection_ts=_T0) == 0


class TestFixDetectionSkewGate:
    """The recorded position must describe where the target was WHEN THE
    NODE DETECTED IT, not wherever the live ADS-B fix happens to be by the
    time this call runs.  age_s and CAL_DETECTION_FRESH_S (applied by the
    caller before this function is reached) are both measured against `now`
    and so cannot see this: a fix can be fresh by both and still be an exit
    smear — the live position recorded for a few seconds after the node
    actually lost the target.  Diagnosed on staging 2026-08-10: 32/32
    directional nodes had learned-FOV lobes outside their theoretical wedge
    from exactly this.
    """

    def test_a_skew_past_the_limit_records_nothing(self, nodes):
        detection_ts = _T0
        fix_ts = _T0 + CAL_FIX_DETECTION_SKEW_S + 1.0   # aircraft kept moving
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0,
                                       fix_ts=fix_ts, detection_ts=detection_ts) == 0
        assert _points("cal-a") == 0

    def test_skew_is_symmetric(self, nodes):
        """A fix that is EARLIER than the detection is just as suspect —
        the rule bounds |fix_ts - detection_ts|, not a one-sided lag."""
        detection_ts = _T0
        fix_ts = _T0 - CAL_FIX_DETECTION_SKEW_S - 1.0
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0,
                                       fix_ts=fix_ts, detection_ts=detection_ts) == 0

    def test_a_skew_at_the_limit_is_recorded(self, nodes):
        detection_ts = _T0
        fix_ts = _T0 + CAL_FIX_DETECTION_SKEW_S
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0,
                                       fix_ts=fix_ts, detection_ts=detection_ts) == 2

    def test_a_recorded_point_is_stamped_with_detection_ts_not_fix_ts(self, nodes, monkeypatch):
        """The point must carry the DETECTION's timestamp, not the fix's —
        otherwise the coverage bin's positive-timestamp history (bin_pos_ts,
        which the out-of-wedge open-span rule reads) would still describe
        when the recording pass happened to run, not when the node actually
        saw the target."""
        captured = []

        def _spy(node_id, lat, lon, ts=None):
            captured.append((node_id, lat, lon, ts))

        monkeypatch.setattr(state.node_analytics, "record_calibration_point", _spy)
        detection_ts = _T0
        fix_ts = _T0 + 0.5
        record_adsb_calibration(["cal-a"], 34.9, -82.35, age_s=1.0,
                                fix_ts=fix_ts, detection_ts=detection_ts)
        assert len(captured) == 1
        assert captured[0][3] == detection_ts


class TestBothCallSitesUseIt:
    def test_frame_path_routes_through_the_helper(self):
        # The frame path's call site moved to services.track_gates with the
        # feed split; that module's binding is the one that matters now.
        import services.track_gates as tg
        assert tg.record_adsb_calibration is record_adsb_calibration

    def test_solver_does_not_calibrate_at_all(self):
        # Publish-path calibration is banned outright: attribution rides on
        # the association the coverage polygon is used to judge, and under
        # an active FOV gate it formed a ghost→positive→wider-gate feedback
        # loop (staging 2026-08-09).  If this import reappears, that loop
        # reappears with it.
        import services.tasks.solver as sv
        assert not hasattr(sv, "record_adsb_calibration")
