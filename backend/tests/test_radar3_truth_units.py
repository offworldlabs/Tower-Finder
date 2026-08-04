"""Stage-1 regression: ground-truth altitude units in radar3 verification.

The GT candidate used to pack metres (trail index 2) under the tar1090 key
``alt_baro``; the consumer then multiplied by 0.3048 as if it were feet, so
every GT-matched altitude error was ~3.28x off.  It also hardcoded
``gs: 0``, comparing the solver speed against a phantom stationary truth.
"""

import os
import time
from collections import deque
from types import SimpleNamespace

import orjson

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services.geo import bistatic_delay_us  # noqa: E402
from services.tasks.analytics_refresh import (  # noqa: E402
    _RADAR3_NODE_ID,
    _refresh_radar3_verification,
)

RX = (34.85, -82.40)
TX = (34.90, -82.20)
TARGET = (34.88, -82.35)


def _radar3_cfg() -> dict:
    return {
        "node_id": _RADAR3_NODE_ID,
        "rx_lat": RX[0], "rx_lon": RX[1],
        "tx_lat": TX[0], "tx_lon": TX[1],
    }


def _track(delay_us: float) -> SimpleNamespace:
    return SimpleNamespace(
        latest_delay_us=delay_us,
        wall_clock_ts=time.time(),
        lat=TARGET[0], lon=TARGET[1],
        vel_east=180.0, vel_north=60.0,
        alt_m=10050.0,
    )


class TestGroundTruthAltitudeUnits:
    def setup_method(self):
        state.active_geo_aircraft.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.ground_truth_trails.clear()
        state.ground_truth_meta.clear()

    def _run_with_gt(self, speed_ms=None):
        meta = {"object_type": "aircraft", "is_anomalous": False}
        if speed_ms is not None:
            meta["speed_ms"] = speed_ms
        else:
            meta["speed_ms"] = None
        state.ground_truth_meta["gt1"] = meta
        # Trail index 2 is METRES.
        state.ground_truth_trails["gt1"] = deque(
            [[TARGET[0], TARGET[1], 10000.0, time.time()]]
        )
        delay = bistatic_delay_us(TX[0], TX[1], RX[0], RX[1], TARGET[0], TARGET[1])
        state.active_geo_aircraft["r3trk"] = (_track(delay), _radar3_cfg())
        _refresh_radar3_verification()
        return orjson.loads(state.latest_radar3_verification_bytes)

    def test_gt_altitude_is_metres_not_reconverted_feet(self):
        data = self._run_with_gt(speed_ms=189.7)
        assert data["n_matched"] == 1
        (m,) = data["tracks"]
        # 10 000 m of truth used to come out as 3 048 m (10 000 * 0.3048).
        assert m["truth_alt_m"] == 10000.0
        assert m["altitude_error_m"] == 50.0

    def test_gt_speed_comes_from_meta_not_zero(self):
        data = self._run_with_gt(speed_ms=189.7)
        (m,) = data["tracks"]
        assert m["truth_speed_ms"] == 189.7
        # Solver speed is sqrt(180^2+60^2) ≈ 189.7 → error ≈ 0, not ≈ 190.
        assert m["velocity_error_ms"] < 1.0

    def test_unknown_gt_speed_is_excluded_from_velocity_stats(self):
        data = self._run_with_gt(speed_ms=None)
        (m,) = data["tracks"]
        assert m["truth_speed_ms"] is None
        assert m["velocity_error_ms"] is None
        # The velocity block's denominator counts only matches WITH speed truth.
        assert data["velocity"]["n"] == 0
