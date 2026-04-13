"""Tests for inter-node association logic."""

from analytics.association import (
    NodeGeometry, compute_overlap_zone, find_associations,
    InterNodeAssociator, _bistatic_delay_at, _lla_to_enu,
)


# ── Overlap zone & bistatic delay ────────────────────────────────────────────


class TestOverlapZone:
    def test_overlap_zone_ids(self):
        geo_a = NodeGeometry(
            node_id="assoc-A", rx_lat=33.939, rx_lon=-84.651, rx_alt_km=0.29,
            tx_lat=33.756, tx_lon=-84.331, tx_alt_km=0.49,
            beam_azimuth_deg=135, beam_width_deg=41, max_range_km=50,
        )
        geo_b = NodeGeometry(
            node_id="assoc-B", rx_lat=34.05, rx_lon=-84.4, rx_alt_km=0.3,
            tx_lat=33.85, tx_lon=-84.15, tx_alt_km=0.5,
            beam_azimuth_deg=210, beam_width_deg=41, max_range_km=50,
        )
        zone = compute_overlap_zone(geo_a, geo_b, grid_step_km=5.0)
        assert zone.node_a_id == "assoc-A"
        assert len(zone.delay_pairs) == len(zone.grid_points)

    def test_bistatic_delay_positive(self):
        ref_lat, ref_lon = 33.9, -84.5
        tx_enu = _lla_to_enu(33.756, -84.331, 0.49, ref_lat, ref_lon, 0.0)
        target_enu = (10.0, 10.0, 8.0)
        delay = _bistatic_delay_at(target_enu, tx_enu)
        assert delay > 0
        assert delay < 300


# ── InterNodeAssociator ──────────────────────────────────────────────────────


class TestInterNodeAssociator:
    def _make_assoc(self):
        assoc = InterNodeAssociator(grid_step_km=5.0)
        assoc.register_node("assoc-A", {
            "rx_lat": 33.939, "rx_lon": -84.651, "rx_alt_ft": 950,
            "tx_lat": 33.756, "tx_lon": -84.331, "tx_alt_ft": 1600,
            "fc_hz": 195e6, "beam_width_deg": 41, "max_range_km": 50,
        })
        assoc.register_node("assoc-B", {
            "rx_lat": 34.05, "rx_lon": -84.4, "rx_alt_ft": 980,
            "tx_lat": 33.85, "tx_lon": -84.15, "tx_alt_ft": 1600,
            "fc_hz": 195e6, "beam_width_deg": 41, "max_range_km": 50,
        })
        return assoc

    def test_register_two_nodes(self):
        assoc = self._make_assoc()
        assert len(assoc.node_geometries) == 2

    def test_overlap_summary(self):
        assoc = self._make_assoc()
        summary = assoc.get_overlap_summary()
        assert isinstance(summary, list)

    def test_beam_width_in_geometry(self):
        assoc = self._make_assoc()
        assert assoc.node_geometries["assoc-A"].beam_width_deg == 41

    def test_submit_frame_returns_list(self):
        assoc = self._make_assoc()
        candidates = assoc.submit_frame("assoc-A", {
            "delay": [30.0, 45.0], "doppler": [60.0, -20.0], "snr": [15.0, 10.0],
        }, timestamp_ms=1000)
        assert isinstance(candidates, list)

    def test_submit_both_frames(self):
        assoc = self._make_assoc()
        assoc.submit_frame("assoc-A", {
            "delay": [30.0, 45.0], "doppler": [60.0, -20.0], "snr": [15.0, 10.0],
        }, timestamp_ms=1000)
        candidates = assoc.submit_frame("assoc-B", {
            "delay": [31.0, 46.0], "doppler": [58.0, -22.0], "snr": [14.0, 9.0],
        }, timestamp_ms=1000)
        assert isinstance(candidates, list)

    def test_solver_format(self):
        assoc = self._make_assoc()
        assoc.submit_frame("assoc-A", {
            "delay": [30.0, 45.0], "doppler": [60.0, -20.0], "snr": [15.0, 10.0],
        }, timestamp_ms=1000)
        candidates = assoc.submit_frame("assoc-B", {
            "delay": [31.0, 46.0], "doppler": [58.0, -22.0], "snr": [14.0, 9.0],
        }, timestamp_ms=1000)
        solver_input = assoc.format_candidates_for_solver(candidates)
        assert isinstance(solver_input, list)
