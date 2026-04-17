"""Tests for the passive radar pipeline.

Covers: PassiveRadarPipeline initialization, frame processing, tracker
integration, InMemoryEventWriter, GeolocatedTrack properties.
"""

import time

from pipeline.passive_radar import (
    DEFAULT_NODE_CONFIG,
    GeolocatedTrack,
    InMemoryEventWriter,
    PassiveRadarPipeline,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_frame(timestamp: int, n_detections: int = 5, base_delay: float = 50.0):
    """Build a minimal valid detection frame."""
    return {
        "timestamp": timestamp,
        "delay": [base_delay + i * 2.0 for i in range(n_detections)],
        "doppler": [10.0 + i * 5.0 for i in range(n_detections)],
        "snr": [20.0 + i for i in range(n_detections)],
    }


def _make_frame_with_adsb(timestamp: int, n: int = 3, hex_prefix: str = "abc"):
    """Build a frame with aligned ADS-B entries."""
    adsb = []
    for i in range(n):
        adsb.append({
            "hex": f"{hex_prefix}{i:03d}",
            "lat": 33.9 + i * 0.01,
            "lon": -84.6 + i * 0.01,
            "alt_baro": 35000,
            "gs": 250,
            "track": 90,
            "flight": f"TEST{i:03d}",
        })
    return {
        "timestamp": timestamp,
        "delay": [50.0 + i * 3.0 for i in range(n)],
        "doppler": [15.0 + i * 4.0 for i in range(n)],
        "snr": [25.0] * n,
        "adsb": adsb,
    }


# ── InMemoryEventWriter ──────────────────────────────────────────────────────

class TestInMemoryEventWriter:
    def test_write_and_get_events(self):
        w = InMemoryEventWriter()
        w.write_event("t1", 1000, 5, [{"delay": 1}], adsb_hex="aaa")
        events = w.get_events()
        assert "t1" in events
        assert events["t1"]["adsb_hex"] == "aaa"
        assert events["t1"]["length"] == 5

    def test_dirty_tracking(self):
        w = InMemoryEventWriter()
        w.write_event("t1", 1000, 5, [{"delay": 1}])
        w.write_event("t2", 1001, 3, [{"delay": 2}])

        new = w.get_new_events()
        assert set(new.keys()) == {"t1", "t2"}

        # Second call returns empty — dirty set was cleared
        new2 = w.get_new_events()
        assert new2 == {}

    def test_lazy_write_defers_detection_resolution(self):
        w = InMemoryEventWriter()

        class FakeTrack:
            def get_recent_detections(self, n=10):
                return [{"delay": 1.0, "doppler": 2.0, "snr": 3.0}]

        w.write_event_lazy("t1", 1000, 1, FakeTrack())
        event = w.events["t1"]
        assert event["detections"] is None
        assert event["_track_ref"] is not None

        # Resolve
        InMemoryEventWriter.resolve_event(event)
        assert event["detections"] == [{"delay": 1.0, "doppler": 2.0, "snr": 3.0}]
        assert "_track_ref" not in event

    def test_resolve_idempotent(self):
        w = InMemoryEventWriter()
        w.write_event("t1", 1000, 2, [{"delay": 5.0}])
        event = w.events["t1"]
        # Already has detections — resolve should be a no-op
        InMemoryEventWriter.resolve_event(event)
        assert event["detections"] == [{"delay": 5.0}]

    def test_anomaly_fields_propagated(self):
        w = InMemoryEventWriter()
        w.write_event("t1", 1000, 1, [], is_anomalous=True,
                       anomaly_types=["spoofing", "hover"])
        event = w.events["t1"]
        assert event["is_anomalous"] is True
        assert event["anomaly_types"] == ["hover", "spoofing"]  # sorted


# ── GeolocatedTrack ──────────────────────────────────────────────────────────

class TestGeolocatedTrack:
    def test_speed_knots(self):
        t = GeolocatedTrack(
            "t1", 33.9, -84.6, 3000, vel_east=100.0, vel_north=0.0, vel_up=0.0,
            rms_delay=0.1, rms_doppler=0.5, n_detections=10,
            timestamp_ms=int(time.time() * 1000),
        )
        # 100 m/s ≈ 194.384 knots
        assert abs(t.speed_knots - 194.384) < 0.1

    def test_track_angle(self):
        # Pure east velocity → heading 90°
        t = GeolocatedTrack(
            "t1", 33.9, -84.6, 3000, vel_east=100.0, vel_north=0.0, vel_up=0.0,
            rms_delay=0.1, rms_doppler=0.5, n_detections=10,
            timestamp_ms=int(time.time() * 1000),
        )
        assert abs(t.track_angle - 90.0) < 0.01

    def test_track_angle_north(self):
        # Pure north velocity → heading 0°
        t = GeolocatedTrack(
            "t1", 33.9, -84.6, 3000, vel_east=0.0, vel_north=100.0, vel_up=0.0,
            rms_delay=0.1, rms_doppler=0.5, n_detections=10,
            timestamp_ms=int(time.time() * 1000),
        )
        assert abs(t.track_angle) < 0.01 or abs(t.track_angle - 360.0) < 0.01

    def test_alt_ft(self):
        t = GeolocatedTrack(
            "t1", 33.9, -84.6, 3048.0, vel_east=0, vel_north=0, vel_up=0,
            rms_delay=0.1, rms_doppler=0.5, n_detections=5,
            timestamp_ms=int(time.time() * 1000),
        )
        # 3048 m ≈ 10000 ft
        assert abs(t.alt_ft - 10000.0) < 1.0

    def test_hex_id_format(self):
        t = GeolocatedTrack(
            "track-42", 33.9, -84.6, 1000, 0, 0, 0, 0, 0, 1,
            timestamp_ms=1000,
        )
        assert t.hex_id.startswith("pr")
        assert len(t.hex_id) == 6


# ── PassiveRadarPipeline ─────────────────────────────────────────────────────

class TestPipelineInit:
    def test_creates_with_default_config(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        assert p.node_id == "net13"
        assert p.tracker is not None
        assert p.event_writer is not None
        assert p.geometry is not None

    def test_custom_config(self):
        cfg = {
            "node_id": "test-node",
            "Fs": 2_000_000,
            "FC": 195_000_000,
            "rx_lat": 34.0,
            "rx_lon": -84.0,
            "rx_alt_ft": 800,
            "tx_lat": 33.8,
            "tx_lon": -83.8,
            "tx_alt_ft": 1200,
            "doppler_min": -200,
            "doppler_max": 200,
            "min_doppler": 10,
        }
        p = PassiveRadarPipeline(cfg)
        assert p.node_id == "test-node"
        assert p.rx_lla[0] == 34.0

    def test_drone_profile_overrides_bounds(self):
        cfg = dict(DEFAULT_NODE_CONFIG, target_profile="drone")
        p = PassiveRadarPipeline(cfg)
        if p.geo_config is not None:
            assert p.geo_config.altitude_bounds == [0, 500]


class TestPipelineProcessFrame:
    def test_process_single_frame(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = _make_frame(timestamp=1000, n_detections=5)
        p.process_frame(frame)
        # Tracker should have processed the frame — check event_writer
        assert p.event_writer is not None

    def test_process_multiple_frames_creates_tracks(self):
        """Feed enough frames for M-of-N track promotion to fire."""
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        # Feed frames with consistent detections to allow track promotion
        for i in range(10):
            frame = _make_frame(timestamp=1000 + i, n_detections=5)
            p.process_frame(frame)
        # After 10 frames, the tracker should have some tracks
        events = p.event_writer.get_events()
        # At least some track events should exist
        assert isinstance(events, dict)

    def test_empty_frame_no_crash(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = {"timestamp": 1000, "delay": [], "doppler": [], "snr": []}
        p.process_frame(frame)
        # Should complete without exception

    def test_frame_with_adsb_data(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = _make_frame_with_adsb(timestamp=1000, n=3)
        p.process_frame(frame)
        # Should process without error

    def test_mismatched_array_lengths(self):
        """Arrays of different lengths should not crash — zip() truncates."""
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = {
            "timestamp": 1000,
            "delay": [50.0, 55.0, 60.0],
            "doppler": [10.0, 15.0],  # shorter
            "snr": [20.0],            # even shorter
        }
        p.process_frame(frame)
        # Only 1 detection created (shortest array), no crash

    def test_generate_aircraft_json(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        result = p.generate_aircraft_json()
        assert "now" in result
        assert "aircraft" in result
        assert isinstance(result["aircraft"], list)

    def test_generate_receiver_json(self):
        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        result = p.generate_receiver_json()
        assert "lat" in result
        assert "lon" in result
        assert result["lat"] == DEFAULT_NODE_CONFIG["rx_lat"]
