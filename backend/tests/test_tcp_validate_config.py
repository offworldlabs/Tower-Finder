"""Tests for _validate_node_config() in services/tcp_handler.py"""

import pytest

from services.tcp_handler import _validate_node_config


class TestValidNodeConfigHappyPath:
    """Valid configurations should return None."""

    def test_valid_config_with_rx_lat_rx_lon(self):
        config = {"rx_lat": 40.7128, "rx_lon": -74.0060}
        assert _validate_node_config(config) is None

    def test_valid_config_with_lat_lon(self):
        config = {"lat": 51.5074, "lon": -0.1278}
        assert _validate_node_config(config) is None

    def test_valid_config_with_beam_width_deg(self):
        config = {"rx_lat": 40.7128, "rx_lon": -74.0060, "beam_width_deg": 45.0}
        assert _validate_node_config(config) is None

    def test_valid_config_with_max_range_km(self):
        config = {"rx_lat": 40.7128, "rx_lon": -74.0060, "max_range_km": 100.0}
        assert _validate_node_config(config) is None

    def test_valid_config_with_both_optional_fields(self):
        config = {
            "rx_lat": 40.7128,
            "rx_lon": -74.0060,
            "beam_width_deg": 45.0,
            "max_range_km": 100.0,
        }
        assert _validate_node_config(config) is None

    def test_valid_beam_width_at_boundary_360(self):
        config = {"rx_lat": 40.7128, "rx_lon": -74.0060, "beam_width_deg": 360}
        assert _validate_node_config(config) is None

    def test_valid_beam_width_near_lower_boundary(self):
        config = {"rx_lat": 40.7128, "rx_lon": -74.0060, "beam_width_deg": 0.1}
        assert _validate_node_config(config) is None

    def test_valid_lat_lon_boundaries(self):
        config = {"lat": 90, "lon": 180}
        assert _validate_node_config(config) is None

    def test_valid_lat_lon_negative_boundaries(self):
        config = {"lat": -90, "lon": -180}
        assert _validate_node_config(config) is None


class TestMissingLatLon:
    """Missing lat/lon should return appropriate error."""

    def test_empty_config(self):
        config = {}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result

    def test_only_rx_lat_present(self):
        config = {"rx_lat": 40.7128}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result

    def test_only_rx_lon_present(self):
        config = {"rx_lon": -74.0060}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result

    def test_only_lat_present(self):
        config = {"lat": 40.7128}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result

    def test_only_lon_present(self):
        config = {"lon": -74.0060}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result

    def test_neither_lat_lon_nor_rx_variants_present(self):
        config = {"beam_width_deg": 45, "max_range_km": 100}
        result = _validate_node_config(config)
        assert result is not None
        assert "missing lat/lon" in result


class TestNonNumericLatLon:
    """Non-numeric lat/lon should return appropriate error."""

    def test_rx_lat_string_non_numeric(self):
        config = {"rx_lat": "abc", "rx_lon": -74.0060}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric lat/lon" in result

    def test_rx_lon_string_non_numeric(self):
        config = {"rx_lat": 40.7128, "rx_lon": "xyz"}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric lat/lon" in result

    def test_both_non_numeric(self):
        config = {"lat": "abc", "lon": "xyz"}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric lat/lon" in result

    def test_lat_non_numeric_string(self):
        config = {"lat": "abc", "lon": -74.0060}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric lat/lon" in result

    def test_lon_list_type(self):
        config = {"lat": 40.7128, "lon": [1, 2, 3]}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric lat/lon" in result


class TestOutOfRangeLatLon:
    """Out-of-range lat/lon should return appropriate error."""

    def test_lat_exceeds_upper_bound(self):
        config = {"lat": 91, "lon": 0}
        result = _validate_node_config(config)
        assert result is not None
        assert "lat/lon out of range" in result

    def test_lat_exceeds_lower_bound(self):
        config = {"lat": -91, "lon": 0}
        result = _validate_node_config(config)
        assert result is not None
        assert "lat/lon out of range" in result

    def test_lon_exceeds_upper_bound(self):
        config = {"lat": 0, "lon": 181}
        result = _validate_node_config(config)
        assert result is not None
        assert "lat/lon out of range" in result

    def test_lon_exceeds_lower_bound(self):
        config = {"lat": 0, "lon": -181}
        result = _validate_node_config(config)
        assert result is not None
        assert "lat/lon out of range" in result

    def test_both_out_of_range(self):
        config = {"lat": 100, "lon": 200}
        result = _validate_node_config(config)
        assert result is not None
        assert "lat/lon out of range" in result



class TestBeamWidthValidation:
    """Tests for beam_width_deg validation."""

    def test_beam_width_non_numeric_string(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": "invalid"}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric beam_width_deg" in result

    def test_beam_width_zero(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": 0}
        result = _validate_node_config(config)
        assert result is not None
        assert "beam_width_deg out of range" in result

    def test_beam_width_negative(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": -45}
        result = _validate_node_config(config)
        assert result is not None
        assert "beam_width_deg out of range" in result

    def test_beam_width_exceeds_upper_bound(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": 361}
        result = _validate_node_config(config)
        assert result is not None
        assert "beam_width_deg out of range" in result



class TestMaxRangeKmValidation:
    """Tests for max_range_km validation."""

    def test_max_range_non_numeric_string(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": "abc"}
        result = _validate_node_config(config)
        assert result is not None
        assert "non-numeric max_range_km" in result

    def test_max_range_zero(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": 0}
        result = _validate_node_config(config)
        assert result is not None
        assert "max_range_km must be positive" in result

    def test_max_range_negative(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": -100}
        result = _validate_node_config(config)
        assert result is not None
        assert "max_range_km must be positive" in result



class TestOptionalFieldsAbsent:
    """Optional fields (beam_width_deg, max_range_km) absent should not cause error."""

    def test_no_beam_width_no_max_range(self):
        config = {"lat": 40.7128, "lon": -74.0060}
        result = _validate_node_config(config)
        assert result is None

    def test_beam_width_none_explicitly(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": None}
        result = _validate_node_config(config)
        assert result is None

    def test_max_range_none_explicitly(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": None}
        result = _validate_node_config(config)
        assert result is None

    def test_both_optional_fields_none(self):
        config = {
            "lat": 40.7128,
            "lon": -74.0060,
            "beam_width_deg": None,
            "max_range_km": None,
        }
        result = _validate_node_config(config)
        assert result is None


class TestStringNumberConversion:
    """Lat/lon as strings (numeric) should be converted and validated."""

    def test_lat_lon_as_numeric_strings(self):
        config = {"lat": "40.7128", "lon": "-74.0060"}
        result = _validate_node_config(config)
        assert result is None

    def test_beam_width_as_numeric_string(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": "45.5"}
        result = _validate_node_config(config)
        assert result is None

    def test_max_range_as_numeric_string(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": "100.5"}
        result = _validate_node_config(config)
        assert result is None

    def test_lat_as_integer_string(self):
        config = {"lat": "45", "lon": "-120"}
        result = _validate_node_config(config)
        assert result is None


class TestEdgeCases:
    """Edge cases and corner cases."""

    def test_lat_lon_as_integers(self):
        config = {"lat": 45, "lon": -120}
        result = _validate_node_config(config)
        assert result is None

    def test_exact_boundary_values(self):
        config = {"lat": 0.0, "lon": 0.0}
        result = _validate_node_config(config)
        assert result is None

    def test_extra_fields_ignored(self):
        config = {
            "lat": 40.7128,
            "lon": -74.0060,
            "extra_field": "should be ignored",
            "another_extra": 123,
        }
        result = _validate_node_config(config)
        assert result is None

    def test_rx_variants_take_precedence_over_flat(self):
        config = {"lat": 91, "lon": 200, "rx_lat": 40.7128, "rx_lon": -74.0060}
        result = _validate_node_config(config)
        assert result is None

    def test_very_small_positive_max_range(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": 0.001}
        result = _validate_node_config(config)
        assert result is None

    def test_very_small_positive_beam_width(self):
        config = {"lat": 40.7128, "lon": -74.0060, "beam_width_deg": 0.001}
        result = _validate_node_config(config)
        assert result is None

    def test_large_valid_max_range(self):
        config = {"lat": 40.7128, "lon": -74.0060, "max_range_km": 10000}
        result = _validate_node_config(config)
        assert result is None

    def test_floats_with_many_decimals(self):
        config = {"lat": 40.712847293847, "lon": -74.0060123123123}
        result = _validate_node_config(config)
        assert result is None

    def test_scientific_notation_lat_lon(self):
        config = {"lat": 4.0e1, "lon": -7.4e1}
        result = _validate_node_config(config)
        assert result is None
