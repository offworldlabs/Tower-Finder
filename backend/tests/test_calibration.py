"""One age rule for coverage calibration points.

The frame path and the solve path recorded these independently and disagreed on
the only parameter that matters: the solver gated at 10 s, the frame path used
_fresh_adsb's 60 s display-freshness window and applied no calibration gate at
all.  At 250 m/s that is 15 km of travel recorded against a 5-degree polar grid
whose entire purpose is to say where a node can see.
"""

import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from config.constants import CAL_MAX_ADSB_AGE_S  # noqa: E402
from core import state  # noqa: E402
from services.calibration import record_adsb_calibration  # noqa: E402

_CFG = dict(rx_lat=34.85, rx_lon=-82.40, tx_lat=34.90, tx_lon=-82.30, max_range_km=50, max_bistatic_range_km=60)


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
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0) == 2
        assert _points("cal-a") == 1

    def test_a_fix_at_the_limit_is_recorded(self, nodes):
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=CAL_MAX_ADSB_AGE_S) == 2

    def test_a_stale_fix_is_refused(self, nodes):
        """60 s was what the frame path accepted — 15 km at cruise."""
        assert record_adsb_calibration(nodes, 34.9, -82.35, age_s=60.0) == 0
        assert _points("cal-a") == 0

    def test_the_limit_is_the_solvers_stated_one(self):
        assert CAL_MAX_ADSB_AGE_S == 10.0


class TestPositionGuard:
    def test_a_missing_position_is_refused(self, nodes):
        assert record_adsb_calibration(nodes, None, -82.35, age_s=1.0) == 0
        assert record_adsb_calibration(nodes, 34.9, None, age_s=1.0) == 0

    def test_null_island_is_refused(self, nodes):
        """0,0 is what an absent field parses to, not a position in the Gulf of
        Guinea."""
        assert record_adsb_calibration(nodes, 0, 0, age_s=1.0) == 0

    def test_blank_node_ids_are_skipped(self, nodes):
        assert record_adsb_calibration([None, "", "cal-a"], 34.9, -82.35, age_s=1.0) == 1


class TestFanOut:
    def test_every_contributing_node_gets_the_point(self, nodes):
        record_adsb_calibration(nodes, 34.9, -82.35, age_s=1.0)
        assert _points("cal-a") == 1
        assert _points("cal-b") == 1

    def test_an_empty_node_list_records_nothing(self, nodes):
        assert record_adsb_calibration([], 34.9, -82.35, age_s=1.0) == 0


class TestBothCallSitesUseIt:
    def test_frame_path_routes_through_the_helper(self):
        # The frame path's call site moved to services.track_gates with the
        # feed split; that module's binding is the one that matters now.
        import services.track_gates as tg

        assert tg.record_adsb_calibration is record_adsb_calibration

    def test_solver_routes_through_the_helper(self):
        import services.tasks.solver as sv

        assert sv.record_adsb_calibration is record_adsb_calibration
