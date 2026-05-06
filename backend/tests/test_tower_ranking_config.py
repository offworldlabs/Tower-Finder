"""Additional tower_ranking tests — reload_config + parse_geom edge cases."""

import json
import os

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
