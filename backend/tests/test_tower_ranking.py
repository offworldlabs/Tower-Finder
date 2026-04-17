"""Tests for tower ranking utilities — source detection, band classification, frequency parsing."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from routes.towers import _detect_source  # noqa: E402
from services.tower_ranking import (  # noqa: E402
    bearing_to_cardinal,
    classify_band,
    classify_distance,
    fspl,
    haversine,
    initial_bearing,
    parse_geom,
    parse_user_frequencies,
    watts_to_dbm,
)

# ── Auto source detection ────────────────────────────────────────────────────


class TestDetectSource:
    def test_sydney_au(self):
        assert _detect_source(-33.8688, 151.2093) == "au"

    def test_washington_dc_us(self):
        assert _detect_source(38.8977, -77.0365) == "us"

    def test_toronto_ca(self):
        assert _detect_source(43.6532, -79.3832) == "ca"

    def test_anchorage_us(self):
        assert _detect_source(61.2181, -149.9003) == "us"

    def test_honolulu_us(self):
        assert _detect_source(21.3069, -157.8583) == "us"

    def test_unknown_fallback_us(self):
        assert _detect_source(0, 0) == "us"


# ── Broadcast band classification ────────────────────────────────────────────


class TestClassifyBand:
    def test_fm_low_edge(self):
        assert classify_band(87.8) == "FM"

    def test_fm_high_edge(self):
        assert classify_band(108.0) == "FM"

    def test_fm_mid(self):
        assert classify_band(95.5) == "FM"

    def test_below_fm(self):
        assert classify_band(87.7) is None

    def test_vhf_low_edge(self):
        assert classify_band(174) == "VHF"

    def test_vhf_high_edge(self):
        assert classify_band(216) == "VHF"

    def test_vhf_mid(self):
        assert classify_band(195) == "VHF"

    def test_gap_returns_none(self):
        assert classify_band(140) is None

    def test_uhf_low_edge(self):
        assert classify_band(470) == "UHF"

    def test_uhf_high_edge(self):
        assert classify_band(608) == "UHF"

    def test_uhf_mid(self):
        assert classify_band(550) == "UHF"

    def test_above_uhf(self):
        assert classify_band(609) is None


# ── User frequency parsing ───────────────────────────────────────────────────


class TestParseUserFrequencies:
    def test_empty_string(self):
        assert parse_user_frequencies("") == []

    def test_single_freq(self):
        assert parse_user_frequencies("95.5") == [95.5]

    def test_multiple_freqs(self):
        assert parse_user_frequencies("95.5, 177.5, 500") == [95.5, 177.5, 500]

    def test_trailing_comma(self):
        assert parse_user_frequencies("95.5,") == [95.5]

    def test_invalid_values_skipped(self):
        assert parse_user_frequencies("abc, 95.5, xyz") == [95.5]

    def test_max_10_enforced(self):
        assert len(parse_user_frequencies(",".join(str(i) for i in range(1, 20)))) == 10

    def test_zero_skipped(self):
        assert parse_user_frequencies("0, 95.5") == [95.5]

    def test_negative_skipped(self):
        assert parse_user_frequencies("-5, 95.5") == [95.5]


# ── Haversine ────────────────────────────────────────────────────────────────


class TestHaversine:
    def test_same_point_zero(self):
        assert haversine(0, 0, 0, 0) == 0.0

    def test_known_distance(self):
        # Sydney → Melbourne ≈ 714 km
        d = haversine(-33.87, 151.21, -37.81, 144.96)
        assert 700 < d < 730


# ── Bearing ──────────────────────────────────────────────────────────────────


class TestBearing:
    def test_due_north(self):
        b = initial_bearing(0, 0, 1, 0)
        assert abs(b) < 1.0 or abs(b - 360) < 1.0

    def test_due_east(self):
        b = initial_bearing(0, 0, 0, 1)
        assert abs(b - 90) < 1.0

    def test_cardinal_north(self):
        assert bearing_to_cardinal(0) == "N"

    def test_cardinal_south(self):
        assert bearing_to_cardinal(180) == "S"

    def test_cardinal_wrap(self):
        assert bearing_to_cardinal(359) == "N"


# ── FSPL ─────────────────────────────────────────────────────────────────────


class TestFSPL:
    def test_zero_distance_returns_zero(self):
        assert fspl(0, 100) == 0.0

    def test_zero_freq_returns_zero(self):
        assert fspl(10, 0) == 0.0

    def test_positive_loss(self):
        assert fspl(10, 100) > 0


# ── Watts to dBm ─────────────────────────────────────────────────────────────


class TestWattsToDbm:
    def test_one_watt(self):
        assert abs(watts_to_dbm(1.0) - 30.0) < 0.01

    def test_zero_returns_neg_inf(self):
        assert watts_to_dbm(0) == float("-inf")

    def test_negative_returns_neg_inf(self):
        assert watts_to_dbm(-1) == float("-inf")


# ── Parse geometry ───────────────────────────────────────────────────────────


class TestParseGeom:
    def test_point_wkt(self):
        result = parse_geom({"string": "POINT(151.2 -33.87)"})
        assert result is not None
        lat, lon = result
        assert abs(lat - (-33.87)) < 0.01
        assert abs(lon - 151.2) < 0.01

    def test_none_input(self):
        assert parse_geom(None) is None

    def test_empty_string(self):
        assert parse_geom({"string": ""}) is None

    def test_plain_string(self):
        result = parse_geom("POINT(0 0)")
        assert result is not None


# ── Distance classification ──────────────────────────────────────────────────


class TestClassifyDistance:
    def test_very_far(self):
        assert classify_distance(99999) == "Far"

    def test_returns_string(self):
        assert isinstance(classify_distance(5.0), str)
