"""Additional tower_ranking tests — reload_config + parse_geom edge cases."""

import json
import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import tower_ranking  # noqa: E402


class TestReloadConfig:
    def test_reload_after_file_change(self, tmp_path, monkeypatch):
        """reload_config() picks up new values from tower_config.json."""
        cfg = {
            "receiver": {
                "rx_antenna_gain_dbi": 12.5,
                "sensitivity_dbm": -110.0,
            },
            "broadcast_bands": {
                "FM": [[88.0, 108.0]],
                "VHF": [[174.0, 216.0]],
            },
            "ranking": {
                "band_priority": {"VHF": 0, "FM": 1},
                "distance_classes": [
                    {"label": "near", "min_km": 0, "max_km": 10},
                    {"label": "far", "min_km": 10, "max_km": None},
                ],
                "distance_priority": {"near": 0, "far": 1},
                "sort_order": [{"field": "band_priority", "ascending": True}],
            },
            "search": {
                "default_radius_km": 123,
                "default_limit": 7,
            },
        }

        fake_path = tmp_path / "tower_config.json"
        fake_path.write_text(json.dumps(cfg))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", fake_path)

        original_gain = tower_ranking.RX_ANTENNA_GAIN_DBI
        try:
            tower_ranking.reload_config()
            assert tower_ranking.RX_ANTENNA_GAIN_DBI == 12.5
            assert tower_ranking.SENSITIVITY_DBM == -110.0
            assert tower_ranking.DEFAULT_RADIUS_KM == 123
            assert tower_ranking.DEFAULT_LIMIT == 7
            # "far" has max_km=None → converted to inf
            far = next(dc for dc in tower_ranking.DISTANCE_CLASSES if dc[0] == "far")
            assert far[2] == float("inf")
        finally:
            # Restore real config so downstream tests aren't broken
            from core.runtime_config import runtime_path

            monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", runtime_path("tower_config.json"))
            tower_ranking.reload_config()
            assert original_gain == tower_ranking.RX_ANTENNA_GAIN_DBI


class TestParseGeomEdgeCases:
    def test_point_well_formed(self):
        # WKT POINT is "lon lat", parse_geom returns (lat, lon)
        assert tower_ranking.parse_geom("POINT(151.2 -33.9)") == (-33.9, 151.2)

    def test_point_wrapped_dict(self):
        assert tower_ranking.parse_geom({"string": "POINT(10 20)"}) == (20.0, 10.0)

    def test_point_missing_paren(self):
        """Malformed WKT used to raise ValueError; now returns None."""
        assert tower_ranking.parse_geom("POINT 10 20") is None
        assert tower_ranking.parse_geom("POINT(10 20") is None

    def test_point_non_numeric(self):
        assert tower_ranking.parse_geom("POINT(x y)") is None

    def test_empty_inputs(self):
        assert tower_ranking.parse_geom(None) is None
        assert tower_ranking.parse_geom("") is None
        assert tower_ranking.parse_geom("   ") is None
        assert tower_ranking.parse_geom({}) is None
        assert tower_ranking.parse_geom(12345) is None

    def test_unknown_geometry(self):
        assert tower_ranking.parse_geom("LINESTRING(0 0, 1 1)") is None

    def test_polygon_centroid(self):
        wkt = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"
        result = tower_ranking.parse_geom(wkt)
        assert result is not None
        lat, lon = result
        # Centroid of unit square (with duplicated closing vertex) ≈ (4, 4)
        assert 3.0 <= lat <= 5.0
        assert 3.0 <= lon <= 5.0

    def test_multipolygon(self):
        wkt = "MULTIPOLYGON(((0 0, 2 0, 2 2, 0 2, 0 0)))"
        result = tower_ranking.parse_geom(wkt)
        assert result is not None


class TestParseUserFrequenciesEdgeCases:
    def test_out_of_range_filtered(self):
        # 0 and >=10000 MHz are invalid; 100 is valid.
        result = tower_ranking.parse_user_frequencies("0, 100, 99999")
        assert result == [100.0]

    def test_max_count_enforced(self):
        raw = ",".join(str(i) for i in range(1, 20))
        result = tower_ranking.parse_user_frequencies(raw, max_count=3)
        assert len(result) == 3

    def test_whitespace_and_empty(self):
        assert tower_ranking.parse_user_frequencies("  ") == []
        assert tower_ranking.parse_user_frequencies(None) == []
        assert tower_ranking.parse_user_frequencies("abc, 88.5, xyz") == [88.5]


# ── Config validation + fail-soft reload ─────────────────────────────────────


@pytest.fixture()
def scratch_config(tmp_path):
    """Point reload_config() at a scratch overlay, restoring the real one after.

    Saves and restores the attribute directly rather than going through
    monkeypatch: monkeypatch.undo() reverts every patch registered on the
    per-test instance, which the fixture shares with the test function, so a
    test that patches something of its own would have it torn down here.
    """
    real = tower_ranking._CONFIG_PATH
    path = tmp_path / "tower_config.json"
    tower_ranking._CONFIG_PATH = path
    try:
        yield path
    finally:
        tower_ranking._CONFIG_PATH = real
        tower_ranking.reload_config()


def _shipped_default() -> dict:
    # The production helper, so the test cannot drift from what the code calls
    # "the shipped default".
    return tower_ranking._default_config()


class TestValidateConfig:
    def test_shipped_default_is_valid(self):
        assert tower_ranking.validate_config(_shipped_default()) is None

    def test_empty_config_is_valid(self):
        # Every section is optional — apply_config() has a default for each.
        assert tower_ranking.validate_config({}) is None

    def test_distance_class_missing_max_km_rejected(self):
        cfg = {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": 8}]}}
        assert "max_km" in tower_ranking.validate_config(cfg)

    def test_open_ended_distance_class_accepted(self):
        # A null max_km is the final open-ended class; apply maps it to inf.
        cfg = {"ranking": {"distance_classes": [{"label": "Far", "min_km": 60, "max_km": None}]}}
        assert tower_ranking.validate_config(cfg) is None

    def test_non_numeric_min_km_rejected(self):
        cfg = {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": "eight", "max_km": 30}]}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_non_object_section_rejected(self):
        assert tower_ranking.validate_config({"receiver": "6 dBi"}) is not None

    def test_broadcast_band_range_must_be_a_pair(self):
        assert tower_ranking.validate_config({"broadcast_bands": {"FM": [[88.0]]}}) is not None

    def test_sort_order_field_must_be_sortable(self):
        # _sort_key() negates a descending value, so a string field raises
        # TypeError on every search: the same "valid shape, breaks at request
        # time" fault as an unguarded NaN.
        cfg = {"ranking": {"sort_order": [{"field": "callsign", "ascending": False}]}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_sort_order_field_must_be_a_string(self):
        # An unhashable value makes the allowlist membership test raise, which
        # would turn the 400 this function exists to produce into a 500.
        cfg = {"ranking": {"sort_order": [{"field": []}]}}
        assert "must be a string" in tower_ranking.validate_config(cfg)

    def test_sort_order_rejects_an_unknown_field(self):
        cfg = {"ranking": {"sort_order": [{"field": "recieved_power_dbm"}]}}
        assert "must be one of" in tower_ranking.validate_config(cfg)

    def test_sort_order_ascending_must_be_a_bool(self):
        cfg = {"ranking": {"sort_order": [{"field": "distance_km", "ascending": "false"}]}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_sort_order_accepts_the_shipped_fields(self):
        cfg = {"ranking": {"sort_order": _shipped_default()["ranking"]["sort_order"]}}
        assert tower_ranking.validate_config(cfg) is None

    def test_every_sortable_field_is_accepted(self):
        for field in tower_ranking._SORTABLE_FIELDS:
            cfg = {"ranking": {"sort_order": [{"field": field, "ascending": False}]}}
            assert tower_ranking.validate_config(cfg) is None, field

    def test_nan_rejected(self):
        # json.loads accepts the bare NaN literal, and every comparison against
        # NaN is False, so an unguarded NaN slips past each range check and is
        # persisted. DEFAULT_LIMIT = nan then makes every search raise TypeError.
        cfg = json.loads('{"search": {"default_limit": NaN}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_infinity_rejected(self):
        cfg = json.loads('{"receiver": {"sensitivity_dbm": -Infinity}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_nan_distance_bound_rejected(self):
        # max_km <= min_km is False when either side is NaN, so the ordering
        # check cannot catch this one on its own.
        cfg = json.loads('{"ranking": {"distance_classes": [{"label": "A", "min_km": NaN, "max_km": 10}]}}')
        assert tower_ranking.validate_config(cfg) is not None


class TestApplyConfig:
    def test_raises_on_a_shape_it_cannot_apply(self):
        """PUT /api/config depends on this raising, to reject the write."""
        with pytest.raises(KeyError):
            tower_ranking.apply_config({"ranking": {"distance_classes": [{"label": "A", "min_km": 8}]}})

    def test_failed_apply_leaves_the_previous_config_intact(self):
        before = (tower_ranking.RX_ANTENNA_GAIN_DBI, list(tower_ranking.DISTANCE_CLASSES))

        with pytest.raises(KeyError):
            tower_ranking.apply_config(
                {
                    "receiver": {"rx_antenna_gain_dbi": 99.0},
                    "ranking": {"distance_classes": [{"label": "A", "min_km": 8}]},
                }
            )

        # The receiver gain is read before the distance classes are built, so an
        # apply that assigned as it went would have taken 99.0 on its way out.
        assert before == (tower_ranking.RX_ANTENNA_GAIN_DBI, tower_ranking.DISTANCE_CLASSES)

    def test_default_sort_order_leads_with_coverage(self, scratch_config):
        """A config with no sort_order of its own gets the coverage-first default.

        services/tower_coverage.py populates coverage_area_added_km2 and this
        default is what makes it count. Every config in the repo sets sort_order
        explicitly, so nothing else exercises the default.
        """
        scratch_config.write_text("{}")

        tower_ranking.reload_config()

        assert tower_ranking.SORT_ORDER[0] == {"field": "coverage_area_added_km2", "ascending": False}


class TestReloadConfigFailSoft:
    """A bad overlay must degrade to defaults, not stop the process booting.

    reload_config() runs at import and the overlay is a named docker volume,
    so raising here is an outage that survives both restart and redeploy.
    """

    def test_malformed_config_falls_back_to_shipped_defaults(self, scratch_config):
        scratch_config.write_text(json.dumps({"ranking": {"distance_classes": [{"label": "Ideal", "min_km": 8}]}}))

        tower_ranking.reload_config()

        expected = [dc["label"] for dc in _shipped_default()["ranking"]["distance_classes"]]
        assert [dc[0] for dc in tower_ranking.DISTANCE_CLASSES] == expected

    def test_invalid_json_falls_back_to_shipped_defaults(self, scratch_config):
        scratch_config.write_text("{ not json")

        tower_ranking.reload_config()

        assert _shipped_default()["search"]["default_limit"] == tower_ranking.DEFAULT_LIMIT

    def test_never_raises_without_shipped_defaults(self, scratch_config, monkeypatch):
        """Last resort: no usable overlay and no shipped default, still boots."""
        scratch_config.write_text("{ not json")
        monkeypatch.setattr(tower_ranking, "_default_config", lambda: None)

        tower_ranking.reload_config()

        assert tower_ranking.DISTANCE_CLASSES == []

    def test_fallback_records_a_reason(self, scratch_config):
        scratch_config.write_text("{ not json")

        tower_ranking.reload_config()

        assert "JSONDecodeError" in tower_ranking.config_fallback_reason

    def test_a_good_config_clears_the_reason(self, scratch_config):
        scratch_config.write_text("{ not json")
        tower_ranking.reload_config()

        scratch_config.write_text("{}")
        tower_ranking.reload_config()

        assert tower_ranking.config_fallback_reason is None

    def test_degraded_config_reaches_the_health_check(self, scratch_config):
        """Otherwise the fallback is one ERROR line at boot and nothing else."""
        from services.health import compute_health_issues

        scratch_config.write_text("{ not json")
        tower_ranking.reload_config()

        assert "config_degraded" in {i["type"] for i in compute_health_issues()}
