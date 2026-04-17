"""Unit tests for the multi-node geolocation solver."""

import numpy as np
import pytest
from retina_geolocator.bistatic_models import bistatic_delay, bistatic_doppler
from retina_geolocator.multinode_solver import (
    MultiNodeMeasurement,
    NodeSetup,
    _enu_km_to_lla,
    _lla_to_enu_km,
    _residual_function,
    solve_multinode,
)

# ── Coordinate conversions ────────────────────────────────────────────────────


class TestCoordinateConversions:
    def test_lla_to_enu_origin_is_zero(self):
        """Reference point maps to (0, 0, ~0)."""
        e, n, u = _lla_to_enu_km(40.0, -74.0, 0.0, 40.0, -74.0, 0.0)
        assert abs(e) < 1e-6
        assert abs(n) < 1e-6
        assert abs(u) < 1e-3

    def test_lla_to_enu_north_offset(self):
        """1 degree north ≈ 111 km north."""
        e, n, u = _lla_to_enu_km(41.0, -74.0, 0.0, 40.0, -74.0, 0.0)
        assert abs(e) < 1.0  # east should be near zero
        assert 110 < n < 112  # ~111 km per degree latitude

    def test_roundtrip_lla_enu_lla(self):
        """LLA → ENU → LLA round-trip preserves coordinates."""
        lat, lon, alt = 48.8566, 2.3522, 5000.0  # Paris, 5km alt
        ref_lat, ref_lon = 48.8, 2.3
        e, n, u = _lla_to_enu_km(lat, lon, alt, ref_lat, ref_lon, 0.0)
        lat2, lon2, alt2 = _enu_km_to_lla(e, n, u, ref_lat, ref_lon, 0.0)
        assert abs(lat2 - lat) < 1e-4
        assert abs(lon2 - lon) < 1e-4
        assert abs(alt2 - alt) < 10.0  # within 10m


# ── Bistatic models ───────────────────────────────────────────────────────────


class TestBistaticModels:
    def test_delay_target_on_baseline_is_zero(self):
        """Target on the TX-RX baseline has zero bistatic delay."""
        # TX at (10, 0, 0), RX at (0, 0, 0), target at midpoint (5, 0, 0)
        delay = bistatic_delay((5, 0, 0), (10, 0, 0), (0, 0, 0))
        assert abs(delay) < 1e-6

    def test_delay_increases_with_offset(self):
        """Target further from baseline has larger delay."""
        tx = (20, 0, 0)
        rx = (0, 0, 0)
        d_near = bistatic_delay((10, 5, 0), tx, rx)
        d_far = bistatic_delay((10, 20, 0), tx, rx)
        assert d_far > d_near

    def test_delay_symmetric(self):
        """Swapping TX and RX gives same delay."""
        target = (5, 10, 3)
        tx, rx = (20, 0, 0), (0, 0, 0)
        d1 = bistatic_delay(target, tx, rx)
        d2 = bistatic_delay(target, rx, tx)
        assert abs(d1 - d2) < 1e-10

    def test_doppler_stationary_is_zero(self):
        """Stationary target has zero Doppler."""
        doppler = bistatic_doppler(
            (10, 10, 5), (0, 0, 0),  # target, vel=0
            (20, 0, 0), (0, 0, 0),   # TX, RX
            100e6,                     # fc
        )
        assert abs(doppler) < 1e-6

    def test_doppler_nonzero_for_moving_target(self):
        """Moving target produces non-zero Doppler."""
        doppler = bistatic_doppler(
            (5, 10, 5), (0, 200, 0),  # 200 m/s north, offset from baseline
            (20, 0, 0), (0, 0, 0),
            100e6,
        )
        assert abs(doppler) > 1.0


# ── Residual function ─────────────────────────────────────────────────────────


class TestResidualFunction:
    @pytest.fixture
    def two_node_setup(self):
        """Create a simple 2-node geometry for testing."""
        setups = {
            "node_a": NodeSetup("node_a", (0, 0, 0), (20, 0, 0), 100e6),
            "node_b": NodeSetup("node_b", (0, 10, 0), (20, 10, 0), 100e6),
        }
        return setups

    def test_perfect_state_has_small_residuals(self, two_node_setup):
        """If measurements match the state perfectly, residuals are near zero."""
        state = np.array([10, 5, 5, 100, 50, 0], dtype=float)
        # Generate synthetic measurements from the state itself
        measurements = []
        for nid, ns in two_node_setup.items():
            d = bistatic_delay(state[:3], ns.tx_enu, ns.rx_enu)
            f = bistatic_doppler(state[:3], state[3:6], ns.tx_enu, ns.rx_enu, ns.fc_hz)
            measurements.append(MultiNodeMeasurement(nid, d, f, snr=10.0))
        res = _residual_function(state, two_node_setup, measurements)
        # Delay and doppler residuals should be near zero
        assert np.max(np.abs(res[:-1])) < 1e-6
        # Altitude constraint: 5 km is in range, should be 0
        assert abs(res[-1]) < 1e-6

    def test_altitude_below_ground_penalty(self, two_node_setup):
        """Below-ground altitude gets penalized."""
        state = np.array([10, 5, 0.01, 0, 0, 0], dtype=float)
        meas = [MultiNodeMeasurement("node_a", 50, 0, snr=10)]
        res = _residual_function(state, two_node_setup, meas)
        alt_penalty = res[-1]
        assert alt_penalty > 1.0  # strong penalty for 0.01 km < 0.05 km

    def test_altitude_above_ceiling_penalty(self, two_node_setup):
        """Above-ceiling altitude gets penalized."""
        state = np.array([10, 5, 20.0, 0, 0, 0], dtype=float)
        meas = [MultiNodeMeasurement("node_a", 50, 0, snr=10)]
        res = _residual_function(state, two_node_setup, meas)
        alt_penalty = res[-1]
        assert alt_penalty > 1.0  # strong penalty for 20 km > 15 km

    def test_snr_weighting(self, two_node_setup):
        """Higher SNR gives larger residuals for same offset."""
        state = np.array([10, 5, 5, 0, 0, 0], dtype=float)
        m_low = [MultiNodeMeasurement("node_a", 100, 50, snr=5)]
        m_high = [MultiNodeMeasurement("node_a", 100, 50, snr=30)]
        res_low = _residual_function(state, two_node_setup, m_low)
        res_high = _residual_function(state, two_node_setup, m_high)
        # High SNR capped at 3.0 weight, low at 0.5 → high residuals are larger
        assert np.sum(res_high[:2] ** 2) > np.sum(res_low[:2] ** 2)


# ── solve_multinode ───────────────────────────────────────────────────────────


class TestSolveMultinode:
    @pytest.fixture
    def two_node_configs(self):
        """Two-node config with realistic geometry around NYC area."""
        return {
            "node_a": {
                "rx_lat": 40.7128, "rx_lon": -74.0060, "rx_alt_ft": 100,
                "tx_lat": 40.78, "tx_lon": -73.95, "tx_alt_ft": 500,
                "fc_hz": 100e6,
            },
            "node_b": {
                "rx_lat": 40.75, "rx_lon": -73.90, "rx_alt_ft": 150,
                "tx_lat": 40.70, "tx_lon": -73.85, "tx_alt_ft": 400,
                "fc_hz": 100e6,
            },
        }

    def _make_synthetic_input(self, node_configs, target_lat, target_lon, target_alt_km,
                              vel_east=0.0, vel_north=0.0, vel_up=0.0):
        """Generate a realistic solver_input from known target position."""
        ref_lat = target_lat
        ref_lon = target_lon

        target_enu = _lla_to_enu_km(target_lat, target_lon, target_alt_km * 1000,
                                     ref_lat, ref_lon, 0.0)
        measurements = []
        for nid, cfg in node_configs.items():
            rx_enu = _lla_to_enu_km(cfg["rx_lat"], cfg["rx_lon"],
                                     cfg["rx_alt_ft"] * 0.3048,
                                     ref_lat, ref_lon, 0.0)
            tx_enu = _lla_to_enu_km(cfg["tx_lat"], cfg["tx_lon"],
                                     cfg["tx_alt_ft"] * 0.3048,
                                     ref_lat, ref_lon, 0.0)
            fc = cfg.get("fc_hz", 100e6)

            delay = bistatic_delay(target_enu, tx_enu, rx_enu)
            doppler = bistatic_doppler(target_enu, (vel_east, vel_north, vel_up),
                                       tx_enu, rx_enu, fc)
            measurements.append({
                "node_id": nid,
                "delay_us": delay,
                "doppler_hz": doppler,
                "snr": 15.0,
            })

        return {
            "initial_guess": {
                "lat": target_lat + 0.01,  # slightly off to test convergence
                "lon": target_lon + 0.01,
                "alt_km": target_alt_km,
            },
            "measurements": measurements,
            "n_nodes": len(node_configs),
            "timestamp_ms": 1700000000000,
        }

    def test_happy_path_two_nodes(self, two_node_configs):
        """Solver converges to correct position with clean 2-node data."""
        target_lat, target_lon, target_alt = 40.73, -73.95, 8.0
        s_in = self._make_synthetic_input(two_node_configs, target_lat, target_lon, target_alt)
        result = solve_multinode(s_in, two_node_configs)

        assert result is not None
        assert result["success"] is True
        assert abs(result["lat"] - target_lat) < 0.05  # within ~5 km
        assert abs(result["lon"] - target_lon) < 0.05
        assert result["n_nodes"] == 2
        assert result["timestamp_ms"] == 1700000000000

    def test_single_measurement_returns_none(self, two_node_configs):
        """Solver returns None with fewer than 2 measurements."""
        s_in = {
            "initial_guess": {"lat": 40.7, "lon": -74.0, "alt_km": 8},
            "measurements": [
                {"node_id": "node_a", "delay_us": 50, "doppler_hz": 10, "snr": 15},
            ],
            "n_nodes": 1,
            "timestamp_ms": 0,
        }
        result = solve_multinode(s_in, two_node_configs)
        assert result is None

    def test_empty_measurements_returns_none(self, two_node_configs):
        """Solver returns None with empty measurement list."""
        s_in = {
            "initial_guess": {"lat": 40.7, "lon": -74.0, "alt_km": 8},
            "measurements": [],
            "n_nodes": 0,
            "timestamp_ms": 0,
        }
        result = solve_multinode(s_in, two_node_configs)
        assert result is None

    def test_missing_node_config_returns_none(self):
        """Solver returns None when node configs don't match measurements."""
        s_in = {
            "initial_guess": {"lat": 40.7, "lon": -74.0, "alt_km": 8},
            "measurements": [
                {"node_id": "ghost_a", "delay_us": 50, "doppler_hz": 10, "snr": 15},
                {"node_id": "ghost_b", "delay_us": 60, "doppler_hz": -5, "snr": 12},
            ],
            "n_nodes": 2,
            "timestamp_ms": 0,
        }
        result = solve_multinode(s_in, {})  # empty configs
        assert result is None

    def test_result_has_velocity(self, two_node_configs):
        """Solver returns velocity components."""
        target = (40.73, -73.95, 8.0)
        s_in = self._make_synthetic_input(
            two_node_configs, *target, vel_east=150.0, vel_north=80.0,
        )
        result = solve_multinode(s_in, two_node_configs)
        assert result is not None
        assert "vel_east" in result
        assert "vel_north" in result
        assert "vel_up" in result

    def test_result_has_fit_quality_metrics(self, two_node_configs):
        """Solver returns RMS delay and doppler fit quality."""
        s_in = self._make_synthetic_input(two_node_configs, 40.73, -73.95, 8.0)
        result = solve_multinode(s_in, two_node_configs)
        assert result is not None
        assert "rms_delay" in result
        assert "rms_doppler" in result
        assert result["rms_delay"] >= 0
        assert result["rms_doppler"] >= 0

    def test_fc_fallback_to_FC_key(self):
        """Solver accepts 'FC' key when 'fc_hz' is missing."""
        configs = {
            "n1": {
                "rx_lat": 40.71, "rx_lon": -74.00, "rx_alt_ft": 100,
                "tx_lat": 40.78, "tx_lon": -73.95, "tx_alt_ft": 500,
                "FC": 195e6,  # uses FC, not fc_hz
            },
            "n2": {
                "rx_lat": 40.75, "rx_lon": -73.90, "rx_alt_ft": 150,
                "tx_lat": 40.70, "tx_lon": -73.85, "tx_alt_ft": 400,
                "FC": 195e6,
            },
        }
        s_in = {
            "initial_guess": {"lat": 40.73, "lon": -73.95, "alt_km": 8},
            "measurements": [
                {"node_id": "n1", "delay_us": 50, "doppler_hz": 10, "snr": 15},
                {"node_id": "n2", "delay_us": 60, "doppler_hz": -5, "snr": 12},
            ],
            "n_nodes": 2,
            "timestamp_ms": 0,
        }
        # Should not raise — FC fallback works
        result = solve_multinode(s_in, configs)
        # Result may or may not converge with arbitrary inputs, but shouldn't crash
        assert result is None or isinstance(result, dict)

    def test_contributing_node_ids_returned(self, two_node_configs):
        """Result includes contributing_node_ids."""
        s_in = self._make_synthetic_input(two_node_configs, 40.73, -73.95, 8.0)
        result = solve_multinode(s_in, two_node_configs)
        assert result is not None
        assert set(result["contributing_node_ids"]) == {"node_a", "node_b"}
