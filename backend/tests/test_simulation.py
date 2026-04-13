"""Tests for simulation subsystem — world, synthetic nodes, frame generation."""

from retina_simulation.world import SimulationWorld, NodeConfig as SimNodeConfig
from retina_simulation.node import NodeConfig as SynNodeConfig, _config_hash, SyntheticNodeGenerator


# ── Simulation World ─────────────────────────────────────────────────────────


class TestSimulationWorld:
    def test_aircraft_populated(self):
        world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
        world.add_node(SimNodeConfig(
            node_id="sim-node-1",
            rx_lat=33.939, rx_lon=-84.651,
            tx_lat=33.756, tx_lon=-84.331,
            beam_width_deg=41, max_range_km=50,
        ))
        for _ in range(50):
            world.step(0.5, mode="anomalous")
        assert len(world.aircraft) >= 5

    def test_beam_width_preserved(self):
        world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
        world.add_node(SimNodeConfig(
            node_id="sim-node-1",
            rx_lat=33.939, rx_lon=-84.651,
            tx_lat=33.756, tx_lon=-84.331,
            beam_width_deg=41, max_range_km=50,
        ))
        assert world.nodes["sim-node-1"].beam_width_deg == 41.0

    def test_generate_frames(self):
        world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
        world.add_node(SimNodeConfig(
            node_id="sim-node-1",
            rx_lat=33.939, rx_lon=-84.651,
            tx_lat=33.756, tx_lon=-84.331,
            beam_width_deg=41, max_range_km=50,
        ))
        for _ in range(50):
            world.step(0.5, mode="anomalous")
        frames = world.generate_all_frames(timestamp_ms=1000)
        assert "sim-node-1" in frames
        frame = frames["sim-node-1"]
        assert isinstance(frame.get("delay"), list)
        assert isinstance(frame.get("doppler"), list)

    def test_aircraft_summary(self):
        world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
        world.add_node(SimNodeConfig(
            node_id="sim-node-1",
            rx_lat=33.939, rx_lon=-84.651,
            tx_lat=33.756, tx_lon=-84.331,
            beam_width_deg=41, max_range_km=50,
        ))
        for _ in range(50):
            world.step(0.5, mode="anomalous")
        summary = world.get_aircraft_summary()
        assert isinstance(summary, list)


# ── Synthetic Node ───────────────────────────────────────────────────────────


class TestSyntheticNodeConfig:
    def test_node_id_prefix(self):
        cfg = SynNodeConfig(node_id="syn-test-01")
        assert cfg.node_id.startswith("syn")

    def test_config_hash_format(self):
        cfg = SynNodeConfig(node_id="syn-test-01")
        h = _config_hash(cfg)
        assert isinstance(h, str)
        assert len(h) == 16

    def test_same_config_same_hash(self):
        cfg = SynNodeConfig(node_id="syn-test-01")
        assert _config_hash(cfg) == _config_hash(cfg)

    def test_different_config_different_hash(self):
        cfg1 = SynNodeConfig(node_id="syn-test-01")
        cfg2 = SynNodeConfig(node_id="syn-test-02")
        assert _config_hash(cfg1) != _config_hash(cfg2)


class TestSyntheticNodeGenerator:
    def test_generate_frame_has_fields(self):
        cfg = SynNodeConfig(node_id="syn-test-01")
        gen = SyntheticNodeGenerator(cfg, mode="adsb")
        frame = gen.generate_frame(timestamp_ms=1000)
        assert "delay" in frame
        assert "doppler" in frame
        assert "snr" in frame
        assert "adsb" in frame
        assert len(frame["delay"]) > 0
