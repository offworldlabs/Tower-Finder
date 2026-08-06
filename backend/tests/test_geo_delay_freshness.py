"""The published measurement (latest_delay_us) must track the NEWEST detection.

Two regressions live here, found via staging probes where arc tracks drew
loci built from delays frozen for 20-40 s while the target's true delay slid
~1 µs/s (multi-km apparent position error):

1. get_recent_detections() returns newest-first, but the solver path read
   geo_detections[-1] — the OLDEST detection in the window — as "latest".
2. latest_delay_us was only written when the (rate-limited, sometimes
   failing) LM solver produced a fresh GeolocatedTrack; between runs the
   published delay never moved even though every frame delivered new
   detections.
"""

from unittest.mock import patch

import pytest

from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline

_NODE_CONFIG = {**DEFAULT_NODE_CONFIG, "node_id": "delay-fresh-node"}


def _event(track_id, detections, adsb_hex=None):
    """Materialized event dict, detections given newest-first."""
    return {
        "track_id": track_id,
        "timestamp": detections[0]["timestamp"],
        "length": len(detections),
        "detections": detections,
        "adsb_hex": adsb_hex,
        "adsb_initialized": bool(adsb_hex),
        "is_anomalous": False,
        "max_velocity_ms": 0.0,
        "anomaly_types": [],
    }


def _det(ts_ms, delay_us, doppler_hz=10.0):
    return {"timestamp": ts_ms, "delay": delay_us, "doppler": doppler_hz,
            "snr": 15.0, "adsb": None}


@pytest.fixture()
def pipe():
    return PassiveRadarPipeline(_NODE_CONFIG)


class TestSolvedTrackUsesNewestDetection:
    def test_latest_delay_is_newest_not_oldest(self, pipe):
        # Newest-first, like get_recent_detections(): 70 µs is the fresh one.
        dets = [_det(3000, 70.0, doppler_hz=25.0),
                _det(2000, 60.0),
                _det(1000, 50.0)]
        fake_solution = {
            "success": True,
            "state": [1.0, 1.0, 5.0, 100.0, 0.0, 0.0],
            "rms_delay": 0.1,
            "rms_doppler": 0.1,
        }
        with patch("pipeline.passive_radar.solve_track", return_value=fake_solution), \
             patch("pipeline.passive_radar.select_initial_guess",
                   return_value=([1.0, 1.0, 5.0, 0.0, 0.0, 0.0], "beam")), \
             patch("pipeline.passive_radar.generate_initial_guess",
                   return_value=[1.0, 1.0, 5.0, 0.0, 0.0, 0.0]):
            result = pipe._geolocate_track_event("trk-1", _event("trk-1", dets))

        assert result is not None
        assert result.latest_delay_us == 70.0
        assert result.latest_doppler_hz == 25.0


class TestDelayRefreshBetweenSolves:
    def _seed_existing(self, pipe, track_id, delay_us):
        """Plant a solved track and arm the solver rate limit."""
        import time
        fake_solution = {
            "success": True,
            "state": [1.0, 1.0, 5.0, 100.0, 0.0, 0.0],
            "rms_delay": 0.1,
            "rms_doppler": 0.1,
        }
        with patch("pipeline.passive_radar.solve_track", return_value=fake_solution), \
             patch("pipeline.passive_radar.select_initial_guess",
                   return_value=([1.0, 1.0, 5.0, 0.0, 0.0, 0.0], "beam")), \
             patch("pipeline.passive_radar.generate_initial_guess",
                   return_value=[1.0, 1.0, 5.0, 0.0, 0.0, 0.0]):
            # min_detections gate: pad the seed event to 3 detections.
            seed = [_det(1000, delay_us), _det(900, delay_us), _det(800, delay_us)]
            pipe.geolocated_tracks[track_id] = pipe._geolocate_track_event(
                track_id, _event(track_id, seed))
        assert pipe.geolocated_tracks[track_id].latest_delay_us == delay_us
        pipe._geo_last_solve[track_id] = time.monotonic()  # rate limit active

    def test_materialized_event_refreshes_delay(self, pipe):
        self._seed_existing(pipe, "trk-2", 60.0)
        pipe.event_writer.write_event(
            "trk-2", 5000, 1, [_det(5000, 71.5, doppler_hz=-4.0)])
        pipe._run_geolocation()
        assert pipe.geolocated_tracks["trk-2"].latest_delay_us == 71.5
        assert pipe.geolocated_tracks["trk-2"].latest_doppler_hz == -4.0

    def test_lazy_event_refreshes_delay_via_track_ref(self, pipe):
        self._seed_existing(pipe, "trk-3", 60.0)

        class _FakeTrack:
            def get_recent_detections(self, n=20):
                return [_det(6000, 33.3, doppler_hz=7.0)]

        pipe.event_writer.write_event_lazy("trk-3", 6000, 1, _FakeTrack())
        pipe._run_geolocation()
        assert pipe.geolocated_tracks["trk-3"].latest_delay_us == 33.3
        assert pipe.geolocated_tracks["trk-3"].latest_doppler_hz == 7.0

    def test_zero_delay_does_not_clobber_last_good_value(self, pipe):
        self._seed_existing(pipe, "trk-4", 60.0)
        pipe.event_writer.write_event(
            "trk-4", 7000, 1, [_det(7000, 0.0)])
        pipe._run_geolocation()
        assert pipe.geolocated_tracks["trk-4"].latest_delay_us == 60.0


class TestAdsbBootstrapCarriesDelay:
    def test_first_encounter_fallback_seeds_delay_from_newest_detection(self, pipe):
        """Solver fails on first encounter (single detection, below the
        min-detections gate) → the ADS-B bootstrap entry must still publish
        the measured delay.  It used to hardcode latest_delay_us=None, so the
        track emitted delay_us=0 (no arc, broken buffer keying) until the
        next refresh interval."""
        from core import state

        state.adsb_aircraft["afh001"] = {
            "hex": "afh001", "lat": 34.0, "lon": -82.0,
            "alt_baro": 30000, "gs": 250, "track": 90,
            "last_seen_ms": 8000,
        }
        try:
            pipe.event_writer.write_event(
                "trk-5", 8000, 1, [_det(8000, 42.5, doppler_hz=3.0)],
                adsb_hex="afh001", adsb_initialized=True)
            pipe._run_geolocation()
            entry = pipe.geolocated_tracks["trk-5"]
            assert entry.adsb_hex == "afh001"
            assert entry.latest_delay_us == 42.5
            assert entry.latest_doppler_hz == 3.0
        finally:
            state.adsb_aircraft.pop("afh001", None)
            with state.geo_aircraft_lock:
                state.active_geo_aircraft.pop("afh001", None)
