"""services/geo.py must reproduce the implementations it replaces.

Written before the old copies are deleted, and kept afterwards: this is what
licenses the deletion.  Six haversines, six bearings, five bistatic-delay
computations and three in-beam checks were independently maintained across the
backend, and the ones that had drifted apart (flat-earth vs spherical, two
different beam-width defaults) are exactly the cases these randomised sweeps
would otherwise let through unnoticed.
"""

import math
import os
import random

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import geo  # noqa: E402
from services import geo as _helpers  # noqa: E402  (shim collapsed; same surface)


def _rng_positions(rng, n, around=(34.85, -82.40), spread=1.2):
    return [(around[0] + rng.uniform(-spread, spread), around[1] + rng.uniform(-spread, spread)) for _ in range(n)]


def _rng_node_cfg(rng):
    rx_lat = 34.85 + rng.uniform(-0.6, 0.6)
    rx_lon = -82.40 + rng.uniform(-0.6, 0.6)
    cfg = {
        "rx_lat": rx_lat,
        "rx_lon": rx_lon,
        "beam_width_deg": rng.choice([30.0, 41.0, 55.0, 75.0]),
        "max_range_km": rng.choice([35.0, 50.0, 60.0]),
    }
    if rng.random() < 0.85:
        cfg["tx_lat"] = rx_lat + rng.uniform(-0.5, 0.5)
        cfg["tx_lon"] = rx_lon + rng.uniform(-0.5, 0.5)
    if rng.random() < 0.6:
        cfg["max_bistatic_range_km"] = rng.choice([30.0, 60.0, 75.0])
    if rng.random() < 0.4:
        cfg["beam_azimuth_deg"] = rng.uniform(0.0, 360.0)
    return cfg


# ── The implementations being replaced, copied verbatim ───────────────────────
# Frozen here rather than imported, so this file keeps testing the *old*
# behaviour after the originals are deleted.

_R_EARTH_KM = 6371.0


def _old_haversine_km(lat1, lon1, lat2, lon2):
    """services/tasks/solver.py:21 — atan2 form."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return _R_EARTH_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _old_bearing_deg_geo(lat1, lon1, lat2, lon2):
    """services/tasks/solver.py:30 — x/y named the other way round."""
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _old_in_node_beam(lat, lon, node_cfg):
    """services/tasks/solver.py:38, verbatim."""
    rx_lat = float(node_cfg.get("rx_lat") or node_cfg.get("lat") or 0)
    rx_lon = float(node_cfg.get("rx_lon") or node_cfg.get("lon") or 0)
    _max_bistatic = node_cfg.get("max_bistatic_range_km")
    _tx_lat = node_cfg.get("tx_lat")
    _tx_lon = node_cfg.get("tx_lon")
    if _max_bistatic is not None and _tx_lat and _tx_lon:
        _r_rx = _old_haversine_km(rx_lat, rx_lon, lat, lon)
        _r_tx = _old_haversine_km(float(_tx_lat), float(_tx_lon), lat, lon)
        _baseline = _old_haversine_km(rx_lat, rx_lon, float(_tx_lat), float(_tx_lon))
        if (_r_rx + _r_tx - _baseline) > float(_max_bistatic):
            return False
    else:
        max_range = float(node_cfg.get("max_range_km") or 50)
        if _old_haversine_km(rx_lat, rx_lon, lat, lon) > max_range:
            return False
    if "beam_azimuth_deg" in node_cfg:
        beam_az = float(node_cfg["beam_azimuth_deg"])
    elif node_cfg.get("tx_lat") and node_cfg.get("tx_lon"):
        beam_az = (
            _old_bearing_deg_geo(rx_lat, rx_lon, float(node_cfg["tx_lat"]), float(node_cfg["tx_lon"])) + 90.0
        ) % 360.0
    else:
        beam_az = None
    if beam_az is None:
        return True
    beam_w = float(node_cfg.get("beam_width_deg") or 41)
    bearing = _old_bearing_deg_geo(rx_lat, rx_lon, lat, lon)
    return abs((bearing - beam_az + 180) % 360 - 180) <= beam_w / 2


# ── Equivalence ───────────────────────────────────────────────────────────────


class TestPrimitives:
    def test_haversine_matches(self):
        rng = random.Random(7)
        for (a, b), (c, d) in zip(_rng_positions(rng, 400), _rng_positions(rng, 400)):
            assert geo.haversine_km(a, b, c, d) == pytest.approx(_old_haversine_km(a, b, c, d), abs=1e-9)

    def test_haversine_matches_the_helpers_asin_form(self):
        """_helpers used asin where solver used atan2 — algebraically the same,
        and this pins that they stay so."""
        rng = random.Random(8)
        for (a, b), (c, d) in zip(_rng_positions(rng, 400), _rng_positions(rng, 400)):
            assert geo.haversine_km(a, b, c, d) == pytest.approx(_helpers.haversine_km(a, b, c, d), abs=1e-9)

    def test_bearing_matches(self):
        rng = random.Random(9)
        for (a, b), (c, d) in zip(_rng_positions(rng, 400), _rng_positions(rng, 400)):
            if (a, b) == (c, d):
                continue
            assert geo.bearing_deg(a, b, c, d) == pytest.approx(_old_bearing_deg_geo(a, b, c, d), abs=1e-9)

    def test_bistatic_delay_matches_the_helper(self):
        rng = random.Random(10)
        for _ in range(300):
            tx, rx, tgt = _rng_positions(rng, 3)
            assert geo.bistatic_delay_us(*tx, *rx, *tgt) == pytest.approx(
                _helpers.bistatic_delay_us(*tx, *rx, *tgt), abs=1e-9
            )


class TestInNodeBeam:
    def test_it_agrees_on_a_randomised_sweep(self):
        """400 node configs x 40 targets. The sweep deliberately includes
        configs with no TX, no bistatic limit, and an explicit azimuth, because
        those are the branches the four copies disagreed on."""
        rng = random.Random(11)
        disagreements = []
        for _ in range(400):
            cfg = _rng_node_cfg(rng)
            for lat, lon in _rng_positions(rng, 40, (cfg["rx_lat"], cfg["rx_lon"])):
                want = _old_in_node_beam(lat, lon, cfg)
                got = geo.in_node_beam(lat, lon, cfg)
                if want != got:
                    disagreements.append((cfg, lat, lon, want, got))
        assert not disagreements, f"{len(disagreements)} of 16000 disagree; first: {disagreements[0]}"

    def test_a_zero_beam_width_is_treated_as_missing(self):
        """The one deliberate divergence: solver read `or 41` and manager read
        `.get(k, YAGI)`. A zero width is always a missing value, never a real
        antenna, so the forgiving form wins in both."""
        cfg = {
            "rx_lat": 34.85,
            "rx_lon": -82.40,
            "tx_lat": 34.90,
            "tx_lon": -82.30,
            "beam_width_deg": 0,
            "max_range_km": 50.0,
        }
        assert geo.node_beam_params(cfg)["beam_width_deg"] == geo.YAGI_BEAM_WIDTH_DEG

    def test_no_tx_and_no_aim_skips_the_bearing_test(self):
        cfg = {"rx_lat": 34.85, "rx_lon": -82.40, "max_range_km": 50.0}
        assert geo.node_beam_params(cfg)["beam_azimuth_deg"] is None
        assert geo.in_node_beam(34.95, -82.30, cfg)

    def test_an_explicit_aim_wins_over_broadside(self):
        cfg = {"rx_lat": 34.85, "rx_lon": -82.40, "tx_lat": 34.90, "tx_lon": -82.30, "beam_azimuth_deg": 123.0}
        assert geo.node_beam_params(cfg)["beam_azimuth_deg"] == 123.0


class TestNodeBeamParamsMalformedInput:
    """node_beam_params is the single authority for a node's beam geometry;
    a review after routing track_gates/analytics_refresh through it found
    three ways malformed config could reach that authority undetected.
    These pin the fixed behaviour for malformed input specifically --
    TestInNodeBeam above (and the randomised sweep in TestPrimitives) already
    cover well-formed input and must stay byte-identical."""

    _RX = (34.85, -82.40)
    _TX = (34.90, -82.30)

    def test_nan_azimuth_with_tx_falls_back_to_real_broadside_not_null_island(self):
        """A present-but-unusable explicit azimuth used to be handed to
        resolve_beam_azimuth_deg with a fabricated (0.0, 0.0) transmitter
        position, so the broadside fallback pointed at Null Island instead
        of this node's real TX. Verified by hand: the buggy output is
        184.36 deg (bearing toward 0,0 from this RX); the real broadside,
        toward the node's actual TX, is 148.61 deg."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "beam_azimuth_deg": float("nan"),
            "max_range_km": 200.0,
        }
        expected = (geo.bearing_deg(*self._RX, *self._TX) + 90.0) % 360.0
        assert geo.node_beam_params(cfg)["beam_azimuth_deg"] == pytest.approx(expected)

    def test_nan_azimuth_without_tx_falls_back_to_none_not_a_fabricated_bearing(self):
        """With no usable TX either, there is no real baseline to point
        broadside along -- the resolved azimuth must be None
        (omnidirectional, callers skip the bearing test), the same as if
        beam_azimuth_deg were absent, and never a bearing computed against a
        made-up transmitter position."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "beam_azimuth_deg": float("nan"),
            "max_range_km": 200.0,
        }
        assert geo.node_beam_params(cfg)["beam_azimuth_deg"] is None

    @pytest.mark.parametrize("bad_az", [float("nan"), float("inf"), float("-inf"), "not-a-number"])
    def test_various_malformed_azimuths_all_fall_back_the_same_way(self, bad_az):
        """NaN, +-inf and non-numeric strings are all "unusable" by the same
        rule resolve_beam_azimuth_deg itself uses -- they must all resolve
        to the real-broadside fallback identically, not just the one shape
        (NaN) the test above exercises."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "beam_azimuth_deg": bad_az,
            "max_range_km": 200.0,
        }
        expected = (geo.bearing_deg(*self._RX, *self._TX) + 90.0) % 360.0
        assert geo.node_beam_params(cfg)["beam_azimuth_deg"] == pytest.approx(expected)

    def test_nan_beam_width_falls_back_to_the_shared_default(self):
        """bool(float('nan')) is True, so the existing `or YAGI_BEAM_WIDTH_DEG`
        truthiness fallback (which already catches None/0 correctly) lets a
        NaN width straight through. Every downstream comparison against it
        is then False (NaN comparisons always are), which fails the
        beam-membership test open instead of closed."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "beam_width_deg": float("nan"),
        }
        assert geo.node_beam_params(cfg)["beam_width_deg"] == geo.YAGI_BEAM_WIDTH_DEG

    def test_infinite_max_range_km_falls_back_to_the_default(self):
        """Same shape as beam_width_deg immediately above it in node_beam_params.
        Asserted against the named geo.YAGI_MAX_RANGE_KM constant, not a bare
        50.0 literal, so this also pins that the fallback reads the shared
        constant rather than a value that happens to agree with it today."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "max_range_km": float("inf"),
        }
        assert geo.node_beam_params(cfg)["max_range_km"] == geo.YAGI_MAX_RANGE_KM

    def test_string_zero_max_bistatic_range_is_cast_to_a_falsy_float(self):
        """max_bistatic_range_km used to be returned raw. The string "0" is
        truthy where the float 0.0 is falsy, which flips which branch
        point_in_beam takes on it (see the next test for the consequence)."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "max_bistatic_range_km": "0",
        }
        out = geo.node_beam_params(cfg)["max_bistatic_range_km"]
        assert out == 0.0
        assert isinstance(out, float)

    def test_string_zero_max_bistatic_no_longer_causes_a_false_miss(self):
        """Concrete consequence of the cast above: an aircraft dead-centre of
        the beam, 8 km out (well inside both max_range_km and any real
        bistatic limit) was rejected because point_in_beam's
        `reach > float(max_bistatic_range_km)` is `reach > 0.0`, true for
        any positive reach, once the truthy string "0" routes it into the
        bistatic branch instead of the monostatic one. Target placed via
        geo.offset_latlon along the broadside bearing and verified by hand
        before writing the assertion."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "max_range_km": 200.0,
            "max_bistatic_range_km": "0",
        }
        target_lat, target_lon = 34.78858355163946, -82.3543377559043
        assert geo.in_node_beam(target_lat, target_lon, cfg) is True

    def test_non_finite_max_bistatic_range_km_is_treated_as_absent(self):
        """Non-finite should mean the same as not-configured, not propagate
        as a real (if useless) limit -- see TestBuildSingleNodeArc's
        max-bistatic test in test_arc_builder.py for why that distinction
        has a real downstream effect via track_gates.py."""
        cfg = {
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
            "max_bistatic_range_km": float("nan"),
        }
        assert geo.node_beam_params(cfg)["max_bistatic_range_km"] is None
