"""Unit tests for tower helper functions: _detect_source and parse_user_frequencies."""

from routes.towers import _detect_source
from services.tower_ranking import parse_user_frequencies

# ── Source Detection ─────────────────────────────────────────────────────────


class TestDetectSource:
    """Tests for _detect_source(lat, lon) -> str."""

    def test_australia_sydney(self):
        """Sydney is in the Australian region."""
        assert _detect_source(-33.9, 151.2) == "au"

    def test_canada_toronto(self):
        """Toronto is in the Canadian region."""
        assert _detect_source(43.7, -79.4) == "ca"

    def test_us_mainland_atlanta(self):
        """Atlanta is in the US mainland region."""
        assert _detect_source(33.7, -84.4) == "us"

    def test_us_alaska(self):
        """Alaska is in the US Alaska/Yukon region."""
        assert _detect_source(61.0, -150.0) == "us"

    def test_us_hawaii(self):
        """Hawaii is in the US Hawaii region."""
        assert _detect_source(20.0, -157.0) == "us"

    def test_fallback_pacific_ocean(self):
        """Middle of Pacific Ocean falls back to 'us'."""
        assert _detect_source(0.0, 170.0) == "us"

    def test_fallback_south_america(self):
        """São Paulo (outside defined regions) falls back to 'us'."""
        assert _detect_source(-23.5, -46.6) == "us"


# ── Frequency Parsing ────────────────────────────────────────────────────────


class TestParseUserFrequencies:
    """Tests for parse_user_frequencies(raw, max_count=10) -> list[float]."""

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert parse_user_frequencies("") == []

    def test_whitespace_only(self):
        """Whitespace-only string returns empty list."""
        assert parse_user_frequencies("   ") == []

    def test_single_frequency(self):
        """Single valid frequency is parsed correctly."""
        assert parse_user_frequencies("98.5") == [98.5]

    def test_multiple_frequencies(self):
        """Comma-separated frequencies are all parsed."""
        assert parse_user_frequencies("88.1,101.5,107.3") == [88.1, 101.5, 107.3]

    def test_skip_invalid_token(self):
        """Invalid tokens are skipped without error."""
        assert parse_user_frequencies("88.1,abc,101.5") == [88.1, 101.5]

    def test_skip_zero_and_above_limit(self):
        """Values outside 0 < val < 10000 are skipped."""
        assert parse_user_frequencies("88.1,0,10001,101.5") == [88.1, 101.5]

    def test_max_count_limit(self):
        """max_count parameter limits results correctly."""
        result = parse_user_frequencies("1.0,2.0,3.0,4.0,5.0", max_count=3)
        assert result == [1.0, 2.0, 3.0]

    def test_negative_skipped(self):
        """Negative values are skipped."""
        assert parse_user_frequencies("-5.0,88.1,101.5") == [88.1, 101.5]

    def test_whitespace_around_values(self):
        """Whitespace around values is handled correctly."""
        assert parse_user_frequencies(" 88.1 , 101.5 , 107.3 ") == [88.1, 101.5, 107.3]

    def test_trailing_comma(self):
        """Trailing comma is handled gracefully."""
        assert parse_user_frequencies("88.1,101.5,") == [88.1, 101.5]

    def test_default_max_count_is_10(self):
        """Default max_count is 10."""
        freqs = ",".join(str(float(i)) for i in range(1, 15))
        result = parse_user_frequencies(freqs)
        assert len(result) == 10
        assert result == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    def test_mixed_valid_and_invalid(self):
        """Mix of valid and invalid tokens produces only valid values."""
        result = parse_user_frequencies("88.1,bad,-5,101.5,xyz,0,107.3")
        assert result == [88.1, 101.5, 107.3]
