"""Tests for tower ranking utilities — source detection, band classification, frequency parsing."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from routes.towers import _detect_source  # noqa: E402
from services.tower_ranking import classify_band, parse_user_frequencies  # noqa: E402

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
