"""Tests for the config-driven blah2 bridge."""

import json
import time

import pytest

from core.task_registry import TASK_EXPECTED_INTERVAL_S
from services.blah2_bridge import (
    Blah2ConfigError,
    _build_node,
    _convert_frame,
    config_file_path,
    load_nodes,
    task_key,
)
from services.tcp_handler import is_synthetic_node

MINIMAL = {
    "node_id": "n1",
    "detection_url": "https://example.test/api/detection",
    "rx_lat": 33.9, "rx_lon": -84.6,
    "tx_lat": 33.8, "tx_lon": -84.1,
    "fc_hz": 177_000_000,
}


def _write(tmp_path, payload):
    p = tmp_path / "blah2_nodes.json"
    p.write_text(json.dumps(payload))
    return p


# ── Shipped default ───────────────────────────────────────────────────────────

class TestShippedConfig:
    def test_default_config_loads(self):
        nodes = load_nodes(config_file_path())
        assert {n.node_id for n in nodes} == {"radar3-retnode", "radar3a-retnode"}

    def test_nodes_are_real_not_synthetic(self):
        """Registered with is_synthetic=False, so they must not trip the prefix
        classifier that strips synthetic nodes from the public feed."""
        for node in load_nodes(config_file_path()):
            assert is_synthetic_node(node.node_id) is False

    def test_radar3a_geometry(self):
        """radar3a shares radar3's receiver but is illuminated by WGTV (RF ch 7,
        Stone Mountain), not WXIA — reusing radar3's TX/FC misplaces every target."""
        by_id = {n.node_id: n.config for n in load_nodes(config_file_path())}
        r3, r3a = by_id["radar3-retnode"], by_id["radar3a-retnode"]
        assert (r3a["rx_lat"], r3a["rx_lon"]) == (r3["rx_lat"], r3["rx_lon"])
        assert (r3a["tx_lat"], r3a["tx_lon"]) == (33.805000, -84.144444)
        assert r3a["fc_hz"] == 177_000_000
        assert r3a["fc_hz"] != r3["fc_hz"]

    def test_radar3_tx_is_wxia_not_a_copy_of_rx(self):
        """Regression: tx_lat was once byte-identical to rx_lat, putting the
        illuminator ~20 km north of WXIA-TV and biasing every radar3 solve.
        A wrong coordinate is silent — only the delay residual shows it."""
        r3 = next(n.config for n in load_nodes(config_file_path())
                  if n.node_id == "radar3-retnode")
        assert r3["tx_lat"] != r3["rx_lat"]
        assert (r3["tx_lat"], r3["tx_lon"]) == (33.756667, -84.331944)
        assert r3["fc_hz"] == 195_000_000

    def test_every_node_tx_differs_from_its_rx(self):
        """A bistatic pair with TX on top of RX has no baseline to solve against."""
        for node in load_nodes(config_file_path()):
            c = node.config
            assert (c["tx_lat"], c["tx_lon"]) != (c["rx_lat"], c["rx_lon"]), node.node_id

    def test_registers_a_staleness_key_per_node(self):
        for node in load_nodes(config_file_path()):
            assert task_key(node.node_id) in TASK_EXPECTED_INTERVAL_S


# ── Loading arbitrary configs ─────────────────────────────────────────────────

class TestLoadNodes:
    def test_loads_arbitrary_node_count(self, tmp_path):
        entries = []
        for i in range(5):
            e = dict(MINIMAL)
            e["node_id"] = f"n{i}"
            e["detection_url"] = f"https://host{i}.test/api/detection"
            entries.append(e)
        nodes = load_nodes(_write(tmp_path, {"nodes": entries}))
        assert [n.node_id for n in nodes] == [f"n{i}" for i in range(5)]

    def test_bare_list_is_accepted(self, tmp_path):
        assert len(load_nodes(_write(tmp_path, [MINIMAL]))) == 1

    def test_missing_file_yields_no_nodes(self, tmp_path):
        assert load_nodes(tmp_path / "nope.json") == []

    def test_malformed_json_yields_no_nodes(self, tmp_path):
        p = tmp_path / "blah2_nodes.json"
        p.write_text("{not json")
        assert load_nodes(p) == []

    def test_bad_entry_is_skipped_others_survive(self, tmp_path):
        """One broken node must not take the rest of the network off the air."""
        good = dict(MINIMAL, node_id="good", detection_url="https://g.test/api/detection")
        bad = dict(MINIMAL, node_id="bad", fc_hz="not-a-number")
        nodes = load_nodes(_write(tmp_path, {"nodes": [bad, good]}))
        assert [n.node_id for n in nodes] == ["good"]

    def test_duplicate_node_id_is_skipped(self, tmp_path):
        a = dict(MINIMAL, detection_url="https://a.test/api/detection")
        b = dict(MINIMAL, detection_url="https://b.test/api/detection")
        nodes = load_nodes(_write(tmp_path, {"nodes": [a, b]}))
        assert len(nodes) == 1

    def test_duplicate_url_is_skipped(self, tmp_path):
        a = dict(MINIMAL, node_id="a")
        b = dict(MINIMAL, node_id="b")
        nodes = load_nodes(_write(tmp_path, {"nodes": [a, b]}))
        assert [n.node_id for n in nodes] == ["a"]

    def test_env_override_wins(self, tmp_path, monkeypatch):
        p = _write(tmp_path, {"nodes": [MINIMAL]})
        monkeypatch.setenv("BLAH2_NODES_FILE", str(p))
        assert config_file_path() == p


# ── Entry validation ──────────────────────────────────────────────────────────

class TestBuildNode:
    def test_optional_fields_get_defaults(self):
        cfg = _build_node(MINIMAL).config
        assert cfg["fs_hz"] == 2_000_000
        assert cfg["beam_width_deg"] == 120
        assert cfg["max_range_km"] == 140

    def test_fc_and_fs_aliases_track_each_other(self):
        """The pipeline factory reads FC/Fs, analytics reads fc_hz/fs_hz."""
        cfg = _build_node(MINIMAL).config
        assert cfg["FC"] == cfg["fc_hz"]
        assert cfg["Fs"] == cfg["fs_hz"]

    def test_peer_is_the_hostname(self):
        assert _build_node(MINIMAL).peer == "example.test"

    @pytest.mark.parametrize("mutation,field", [
        ({"node_id": None}, "node_id"),
        ({"detection_url": None}, "detection_url"),
        ({"fc_hz": None}, "fc_hz"),
        ({"rx_lat": None}, "rx_lat"),
    ])
    def test_missing_required_field_rejected(self, mutation, field):
        with pytest.raises(Blah2ConfigError, match=field):
            _build_node({**MINIMAL, **mutation})

    @pytest.mark.parametrize("url", ["ftp://h/a", "h/a", "", "file:///etc/passwd"])
    def test_non_http_url_rejected(self, url):
        with pytest.raises(Blah2ConfigError, match="http"):
            _build_node({**MINIMAL, "detection_url": url})

    @pytest.mark.parametrize("field,value", [
        ("rx_lat", 91), ("tx_lat", -91), ("rx_lon", 181), ("tx_lon", -181),
    ])
    def test_out_of_range_coordinates_rejected(self, field, value):
        with pytest.raises(Blah2ConfigError, match="out of range"):
            _build_node({**MINIMAL, field: value})

    @pytest.mark.parametrize("field", ["fc_hz", "fs_hz"])
    def test_non_positive_frequency_rejected(self, field):
        with pytest.raises(Blah2ConfigError, match="positive"):
            _build_node({**MINIMAL, field: 0})

    def test_non_numeric_rejected(self):
        with pytest.raises(Blah2ConfigError, match="not a number"):
            _build_node({**MINIMAL, "rx_lon": "west"})

    def test_non_dict_entry_rejected(self):
        with pytest.raises(Blah2ConfigError, match="must be an object"):
            _build_node("radar3")


# ── Frame conversion ──────────────────────────────────────────────────────────

class TestConvertFrame:
    def _raw(self, ts_ms):
        return {
            "timestamp": ts_ms,
            "delay": [19.86, 18.27],
            "doppler": [-160.62, -111.43],
            "snr": [10.06, 5.81],
        }

    def test_tags_frame_with_its_own_node_id(self):
        """Frames from different nodes must stay attributable in the shared queue."""
        now_ms = int(time.time() * 1000)
        for node_id in ("radar3-retnode", "radar3a-retnode"):
            assert _convert_frame(self._raw(now_ms), node_id)["_node_id"] == node_id

    def test_delay_converted_km_to_us(self):
        from config.constants import C_KM_US
        raw = self._raw(int(time.time() * 1000))
        frame = _convert_frame(raw, "n1")
        assert frame["delay"] == [d / C_KM_US for d in raw["delay"]]

    def test_stale_frame_rejected(self):
        assert _convert_frame(self._raw(1_000_000), "n1") is None

    def test_empty_frame_rejected(self):
        assert _convert_frame({"timestamp": 0, "delay": []}, "n1") is None
