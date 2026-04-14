"""
Passive Radar Detection Pipeline
Uses retina-tracker (Kalman+GNN) and retina-geolocator (LM solver)
to process detection data and output tar1090-compatible aircraft.json.

Pipeline: detection data → retina-tracker → retina-geolocator → tar1090 JSON
"""

import json
import logging
import math
import os
import time
import glob
import io
from pathlib import Path
from typing import Optional

import yaml

import numpy as np

logger = logging.getLogger(__name__)

from retina_tracker.tracker import Tracker as RetinaTracker
from retina_tracker.output import TrackEventWriter
from retina_tracker.config import set_config as _set_tracker_global_config

from retina_geolocator import (
    Geometry,
    calculate_baseline_geometry,
    generate_initial_guess,
    select_initial_guess,
    solve_track,
    load_config as load_radar_config,
    load_geolocator_config,
    Detection as GeoDetection,
    Track as GeoTrack,
)

# ─── Constants ───────────────────────────────────────────────────────
from config.constants import (
    C_M_S as C, FT_TO_M,
    DRONE_ALTITUDE_BOUNDS, DRONE_VELOCITY_BOUNDS,
    DRONE_INITIAL_ALT_M, DRONE_MAX_SPEED_MS, DRONE_MAX_ALT_M,
    GEO_INTERVAL_S, PRUNE_INTERVAL_S,
)

# ─── Node Configuration ─────────────────────────────────────────────
DEFAULT_NODE_CONFIG = {
    "node_id": "net13",
    "Fs": 2_000_000,        # Sample rate Hz
    "FC": 195_000_000,      # Center frequency Hz
    "rx_lat": 33.939182,
    "rx_lon": -84.651910,
    "rx_alt_ft": 950,
    "tx_lat": 33.75667,
    "tx_lon": -84.331844,
    "tx_alt_ft": 1600,
    "doppler_min": -300,
    "doppler_max": 300,
    "min_doppler": 15,
}


class InMemoryEventWriter:
    """Captures track events in memory instead of writing to file."""

    def __init__(self):
        self.events = {}   # track_id → latest event dict
        self._dirty: set = set()  # track_ids written since last get_new_events()

    def write_event(self, track_id, timestamp, length, detections,
                    adsb_hex=None, adsb_initialized=False,
                    is_anomalous=False, max_velocity_ms=0.0,
                    anomaly_types=None):
        self.events[track_id] = {
            "track_id": track_id,
            "timestamp": timestamp,
            "length": length,
            "detections": detections,
            "adsb_hex": adsb_hex,
            "adsb_initialized": adsb_initialized,
            "is_anomalous": is_anomalous,
            "max_velocity_ms": max_velocity_ms,
            "anomaly_types": sorted(anomaly_types) if anomaly_types else [],
        }
        self._dirty.add(track_id)

    def write_event_lazy(self, track_id, timestamp, length, track_ref,
                         adsb_hex=None, adsb_initialized=False,
                         is_anomalous=False, max_velocity_ms=0.0,
                         anomaly_types=None):
        """Lightweight write_event — stores a track reference instead of
        materializing the detection list.  The full detection list is resolved
        lazily via get_new_events_resolved() only for tracks that actually
        need geolocation.  This avoids ~3200 dict creations per frame from
        get_recent_detections() calls that were previously discarded.
        """
        self.events[track_id] = {
            "track_id": track_id,
            "timestamp": timestamp,
            "length": length,
            "detections": None,       # deferred
            "_track_ref": track_ref,   # for lazy resolution
            "adsb_hex": adsb_hex,
            "adsb_initialized": adsb_initialized,
            "is_anomalous": is_anomalous,
            "max_velocity_ms": max_velocity_ms,
            "anomaly_types": sorted(anomaly_types) if anomaly_types else [],
        }
        self._dirty.add(track_id)

    def get_events(self):
        return self.events

    def get_new_events(self) -> dict:
        """Return only events updated since the last call; clears the dirty set.

        Lazy detection references are NOT resolved here — caller must call
        resolve_event() before passing to the solver.  This avoids
        materializing detection dicts for tracks that will be skipped by
        the ADS-B fast-path.
        """
        if not self._dirty:
            return {}
        result = {}
        for tid in self._dirty:
            event = self.events.get(tid)
            if event is not None:
                result[tid] = event
        self._dirty.clear()
        return result

    @staticmethod
    def resolve_event(event):
        """Materialize lazy detection list on an event dict (idempotent)."""
        if event.get("detections") is None and event.get("_track_ref") is not None:
            track = event["_track_ref"]
            event["detections"] = track.get_recent_detections(n=event["length"])
            event.pop("_track_ref", None)


def _enu_to_lla(enu_km, rx_lat, rx_lon, rx_alt):
    """Convert ENU position (km) to LLA coordinates."""
    enu_m = (enu_km[0] * 1000, enu_km[1] * 1000, enu_km[2] * 1000)
    ecef = Geometry.enu2ecef(enu_m[0], enu_m[1], enu_m[2], rx_lat, rx_lon, rx_alt)
    return Geometry.ecef2lla(ecef[0], ecef[1], ecef[2])


# Target profile constants
_DRONE_ALTITUDE_BOUNDS = DRONE_ALTITUDE_BOUNDS
_DRONE_VELOCITY_BOUNDS = DRONE_VELOCITY_BOUNDS
_DRONE_INITIAL_ALT_M   = DRONE_INITIAL_ALT_M
_DRONE_MAX_SPEED_MS    = DRONE_MAX_SPEED_MS
_DRONE_MAX_ALT_M       = DRONE_MAX_ALT_M


class GeolocatedTrack:
    """A track that has been geolocated by the LM solver."""

    def __init__(self, track_id, lat, lon, alt_m, vel_east, vel_north, vel_up,
                 rms_delay, rms_doppler, n_detections, timestamp_ms,
                 adsb_hex=None, latest_delay_us=None, target_class=None,
                 latest_doppler_hz=None,
                 is_anomalous=False, anomaly_types=None, max_velocity_ms=0.0):
        self.track_id = track_id
        self.hex_id = f"pr{abs(hash(track_id)) % 0xFFFF:04x}"
        self.lat = lat
        self.lon = lon
        self.alt_m = alt_m
        self.vel_east = vel_east    # m/s
        self.vel_north = vel_north  # m/s
        self.vel_up = vel_up        # m/s
        self.rms_delay = rms_delay
        self.rms_doppler = rms_doppler
        self.n_detections = n_detections
        self.last_update_ms = timestamp_ms
        self.wall_clock_ts = time.time()  # wall-clock for staleness checks
        self.pos_fix_ts = self.wall_clock_ts  # when lat/lon actually changed
        self.adsb_hex = adsb_hex
        self.latest_delay_us = latest_delay_us
        self.latest_doppler_hz = latest_doppler_hz
        self.target_class = target_class  # "aircraft", "drone", or None
        self.is_anomalous = is_anomalous
        self.anomaly_types = anomaly_types or set()
        self.max_velocity_ms = max_velocity_ms

    @property
    def speed_knots(self):
        speed_ms = math.sqrt(self.vel_east ** 2 + self.vel_north ** 2)
        return speed_ms * 1.94384

    @property
    def track_angle(self):
        angle = math.degrees(math.atan2(self.vel_east, self.vel_north))
        return angle % 360

    @property
    def alt_ft(self):
        return self.alt_m / FT_TO_M


# ─── Pipeline: Detection → retina-tracker → retina-geolocator → tar1090 ────

class PassiveRadarPipeline:
    """Full pipeline using retina-tracker and retina-geolocator."""

    _BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))


    def __init__(self, node_config: dict = None):
        config = node_config or DEFAULT_NODE_CONFIG
        self.config = config
        self.node_id = config.get("node_id", "net13")

        # Set up retina-tracker with in-memory event writer
        self.event_writer = InMemoryEventWriter()

        # Load tracker config (from installed package location)
        import retina_tracker as _rt_pkg
        tracker_config_path = os.path.join(
            os.path.dirname(_rt_pkg.__file__), "config.yaml"
        )
        tracker_config = {}
        if os.path.exists(tracker_config_path):
            with open(tracker_config_path, "r") as f:
                tracker_config = yaml.safe_load(f)
            # Propagate to the global config so MIN_SNR() / M_THRESHOLD() etc.
            # read the correct values (they use a module-level singleton, not
            # the per-instance config dict stored on the Tracker object).
            _set_tracker_global_config(tracker_config)

        self.tracker = RetinaTracker(
            event_writer=self.event_writer,
            detection_window=tracker_config.get("tracker", {}).get("detection_window", 20),
            config=tracker_config,
        )

        # Set up geolocator
        self._init_geolocator(config)

        # Store geolocated tracks
        self.geolocated_tracks = {}  # track_id → GeolocatedTrack
        self._previous_solutions = {}  # track_id → state vector
        # Per-track geolocation rate-limiter: solve at most once per
        # _GEO_INTERVAL_S to avoid running the expensive LM solver every
        # frame.  Aircraft positions change slowly enough that 5-second
        # re-solve intervals produce identical quality at 50x lower CPU cost.
        self._geo_last_solve: dict[str, float] = {}
        self._GEO_INTERVAL_S: float = GEO_INTERVAL_S
        # Stale-entry pruning: time-based (every _PRUNE_INTERVAL_S wall-clock
        # seconds) instead of frame-count-based.  With 40 s frame intervals
        # the old 500-frame threshold took ~5.5 h to fire; time-based pruning
        # runs reliably regardless of frame rate.
        self._frame_count: int = 0
        self._PRUNE_INTERVAL_S: float = PRUNE_INTERVAL_S
        self._last_prune_time: float = time.monotonic()

    def _init_geolocator(self, config):
        """Initialize geolocator geometry and config."""
        rx_alt_m = config["rx_alt_ft"] * FT_TO_M
        tx_alt_m = config["tx_alt_ft"] * FT_TO_M

        self.rx_lla = (config["rx_lat"], config["rx_lon"], rx_alt_m)
        self.tx_lla = (config["tx_lat"], config["tx_lon"], tx_alt_m)
        self.frequency = config["FC"]

        # Calculate baseline geometry (antenna boresight, baseline distance, etc.)
        self.geometry = calculate_baseline_geometry(self.rx_lla, self.tx_lla)

        # Compute TX position in ENU (km) relative to RX
        tx_ecef = Geometry.lla2ecef(self.tx_lla[0], self.tx_lla[1], self.tx_lla[2])
        tx_enu_m = Geometry.ecef2enu(
            tx_ecef[0], tx_ecef[1], tx_ecef[2],
            self.rx_lla[0], self.rx_lla[1], self.rx_lla[2],
        )
        self.tx_enu = (tx_enu_m[0] / 1000, tx_enu_m[1] / 1000, tx_enu_m[2] / 1000)
        self.rx_enu = (0, 0, 0)

        # Load geolocator config (from installed package location)
        import retina_geolocator as _rg_pkg
        geo_config_path = os.path.join(
            os.path.dirname(_rg_pkg.__file__), "geolocator_config.yml"
        )
        if os.path.exists(geo_config_path):
            self.geo_config = load_geolocator_config(geo_config_path)
        else:
            self.geo_config = None

        # Apply target-profile overrides to solver initial-guess bounds
        self.target_profile = config.get("target_profile", "aircraft")
        if self.target_profile == "drone" and self.geo_config is not None:
            self.geo_config.altitude_bounds = list(_DRONE_ALTITUDE_BOUNDS)
            self.geo_config.velocity_bounds = list(_DRONE_VELOCITY_BOUNDS)
            self.geo_config.initial_altitude_m = _DRONE_INITIAL_ALT_M

    def _geolocate_track_event(self, track_id, event):
        """Run LM solver on a track event to get lat/lon/alt/velocity."""
        # Resolve lazy detection reference if needed (only for tracks that
        # actually reach the solver — ADS-B fast-path skips this entirely).
        InMemoryEventWriter.resolve_event(event)
        detections_data = event.get("detections", [])

        min_det = 3
        if self.geo_config:
            min_det = self.geo_config.min_detections

        if len(detections_data) < min_det:
            return None

        # Build geolocator Detection objects
        geo_detections = []
        for d in detections_data:
            geo_detections.append(GeoDetection(
                timestamp=d["timestamp"],
                delay=d["delay"],
                doppler=d["doppler"],
                snr=d.get("snr", 0),
                adsb=d.get("adsb"),
            ))

        # Build geolocator Track object
        geo_track = GeoTrack(track_id, geo_detections, event)

        # Always inject the freshest ADS-B position from state.adsb_aircraft
        # for tracks that have an ADS-B hex so the solver uses a current
        # initial guess on every invocation (not stale inline detection data).
        if event.get("adsb_hex"):
            from core import state as _state  # deferred to avoid circular import at module level
            _adsb = _state.adsb_aircraft.get(event["adsb_hex"])
            if _adsb and _adsb.get("lat") and _adsb.get("lon"):
                import time as _t
                age = _t.time() - _adsb.get("last_seen_ms", 0) / 1000
                if age < 60:
                    # Overwrite first detection ADS-B field unconditionally so
                    # select_initial_guess() always starts from fresh coordinates.
                    geo_track.adsb_initialized = True
                    if geo_track.detections:
                        geo_track.detections[0].adsb = {
                            "lat": _adsb["lat"],
                            "lon": _adsb["lon"],
                            "alt_baro": _adsb.get("alt_baro", 0),
                            "gs": _adsb.get("gs", 0),
                            "track": _adsb.get("track", 0),
                        }

        # Generate initial guess
        if (self.geo_config and self.geo_config.temporal_continuity
                and track_id in self._previous_solutions):
            initial_guess = self._previous_solutions[track_id]
        else:
            if self.geo_config:
                initial_guess, _ = select_initial_guess(
                    geo_track,
                    self.tx_enu,
                    self.geometry["antenna_boresight_vector"],
                    self.frequency,
                    self.geo_config,
                    self.rx_lla,
                )
            else:
                initial_guess = generate_initial_guess(
                    geo_track, self.tx_enu,
                    self.geometry["antenna_boresight_vector"],
                    self.frequency,
                )

        # Solve — use fewer evaluations for refinement (temporal continuity)
        is_refinement = (self.geo_config and self.geo_config.temporal_continuity
                         and track_id in self._previous_solutions)
        try:
            result = solve_track(
                geo_track,
                initial_guess,
                self.tx_enu,
                self.rx_enu,
                self.frequency,
                self.geometry["antenna_boresight"],
                self.rx_lla[2],  # rx_alt_m
                max_nfev=10 if is_refinement else 20,
            )
        except Exception as _e:
            logging.debug("Solver raised for track %s: %s", track_id, _e)
            return None

        if not result["success"]:
            return None

        # Store solution for temporal continuity
        self._previous_solutions[track_id] = result["state"]

        # Convert ENU (km) → LLA
        final_enu_km = result["state"][:3]
        lat, lon, alt = _enu_to_lla(final_enu_km, *self.rx_lla)

        # Classify target based on profile and solved kinematics
        speed_ms = math.sqrt(result["state"][3] ** 2 + result["state"][4] ** 2)
        if self.target_profile == "drone":
            target_class = "drone"
        elif self.target_profile == "aircraft":
            target_class = "aircraft"
        else:  # "auto"
            target_class = "drone" if speed_ms <= _DRONE_MAX_SPEED_MS and alt <= _DRONE_MAX_ALT_M else "aircraft"

        # Anomaly detection: inherit tracker flags only.
        # Do NOT flag supersonic based on solver velocity — single-node LM
        # geometry is underdetermined and vel_east/vel_north are too noisy to
        # be a reliable speed source. Legitimate supersonic detections are
        # already caught by the tracker via ADS-B ground speed and propagated
        # through event["anomaly_types"].
        evt_anomalous = event.get("is_anomalous", False)
        evt_max_vel = event.get("max_velocity_ms", 0.0)
        anomaly_types = set(event.get("anomaly_types", []))
        is_anomalous = evt_anomalous or bool(anomaly_types)
        max_velocity_ms = max(speed_ms, evt_max_vel)

        return GeolocatedTrack(
            track_id=track_id,
            lat=lat,
            lon=lon,
            alt_m=alt,
            vel_east=result["state"][3],
            vel_north=result["state"][4],
            vel_up=result["state"][5],
            rms_delay=result["rms_delay"],
            rms_doppler=result["rms_doppler"],
            n_detections=len(geo_detections),
            timestamp_ms=event["timestamp"],
            adsb_hex=event.get("adsb_hex"),
            latest_delay_us=geo_detections[-1].delay if geo_detections else None,
            latest_doppler_hz=geo_detections[-1].doppler if geo_detections else None,
            target_class=target_class,
            is_anomalous=is_anomalous,
            anomaly_types=anomaly_types,
            max_velocity_ms=max_velocity_ms,
        )

    def _prune_stale_tracks(self):
        """Remove dead track IDs from per-track dicts to prevent memory leaks.

        A track ID is considered dead when it no longer appears in the
        event_writer.events dict (no recent write_event call for it) AND it
        is not in the active tracker.tracks list.  Called every
        _PRUNE_EVERY_N frames so the O(N) linear scan does not dominate.
        """
        live_ids = set(self.event_writer.events.keys()) | {
            t.id for t in self.tracker.tracks if t.id
        }
        for d in (self._previous_solutions, self._geo_last_solve, self.geolocated_tracks):
            stale = [k for k in d if k not in live_ids]
            for k in stale:
                del d[k]
        # Prune event_writer.events itself: keep only IDs that are still in
        # tracker.tracks so dead tracks don't accumulate indefinitely.
        active_ids = {t.id for t in self.tracker.tracks if t.id}
        stale_events = [k for k in self.event_writer.events if k not in active_ids]
        for k in stale_events:
            del self.event_writer.events[k]


    def _run_geolocation(self):
        """Run geolocation on tracks that received new data this frame.

        Both ADS-B and non-ADS-B tracks go through the LM solver, rate-limited
        by _GEO_INTERVAL_S.  For ADS-B tracks the solver uses the current ADS-B
        position as a high-quality initial guess (injected in
        _geolocate_track_event), which adds radar-derived accuracy on top of the
        transponder data.  If the solver fails for an ADS-B track, the latest
        ADS-B position is used as a fallback so the track stays visible.

        Between solver runs, ADS-B kinematic metadata (altitude, velocity) is
        refreshed from live state for accurate dead-reckoning on the frontend.
        """
        import time as _time_geo
        from core import state as _state
        now = _time_geo.monotonic()
        for track_id, event in self.event_writer.get_new_events().items():
            adsb_hex = event.get("adsb_hex")
            existing = self.geolocated_tracks.get(track_id)

            # Between solver runs: keep ADS-B kinematic data fresh so the
            # frontend dead-reckoning has current velocity and altitude even
            # when the radar position hasn't been re-solved yet.
            if adsb_hex and existing is not None:
                adsb = _state.adsb_aircraft.get(adsb_hex)
                if adsb:
                    _gs_ms = (adsb.get("gs", 0) or 0) * 0.514444
                    _trk = math.radians(adsb.get("track", 0) or 0)
                    existing.alt_m = (adsb.get("alt_baro", 0) or 0) * FT_TO_M
                    existing.vel_east = _gs_ms * math.sin(_trk)
                    existing.vel_north = _gs_ms * math.cos(_trk)
                    existing.last_update_ms = event["timestamp"]
                    existing.n_detections = event.get("length", existing.n_detections)

            # Rate-limit the solver for all track types.
            if now - self._geo_last_solve.get(track_id, 0.0) < self._GEO_INTERVAL_S:
                continue

            self._geo_last_solve[track_id] = now
            result = self._geolocate_track_event(track_id, event)

            if result is not None:
                # Solver succeeded: use radar-derived position (adds value over ADS-B).
                # Preserve pos_fix_ts when position hasn't actually changed so
                # dead-reckoning elapsed time is not reset spuriously.
                if existing is not None:
                    _pos_changed = (
                        abs(existing.lat - result.lat) > 1e-6
                        or abs(existing.lon - result.lon) > 1e-6
                    )
                    if not _pos_changed:
                        result.pos_fix_ts = existing.pos_fix_ts
                self.geolocated_tracks[track_id] = result
                _hex_key = result.adsb_hex or result.hex_id
                with _state.geo_aircraft_lock:
                    _state.active_geo_aircraft[_hex_key] = (result, self.config)

            elif adsb_hex:
                # Solver failed — fall back to ADS-B position so the track
                # stays on the map.  This is the only path that uses ADS-B
                # coordinates as a position source.
                adsb = _state.adsb_aircraft.get(adsb_hex)
                if adsb and adsb.get("lat") and adsb.get("lon"):
                    _gs_ms = (adsb.get("gs", 0) or 0) * 0.514444
                    _trk = math.radians(adsb.get("track", 0) or 0)
                    if existing is not None:
                        # Only advance pos_fix_ts when position actually changes.
                        _pos_changed = (
                            abs(existing.lat - adsb["lat"]) > 1e-6
                            or abs(existing.lon - adsb["lon"]) > 1e-6
                        )
                        if _pos_changed:
                            existing.lat = adsb["lat"]
                            existing.lon = adsb["lon"]
                            existing.wall_clock_ts = _time_geo.time()
                            existing.pos_fix_ts = existing.wall_clock_ts
                    else:
                        # First encounter and solver failed — bootstrap from ADS-B.
                        _fb_anomaly_types = set(event.get("anomaly_types", []))
                        if _gs_ms > 343.0:
                            _fb_anomaly_types.add("supersonic")
                        existing = GeolocatedTrack(
                            track_id=track_id,
                            lat=adsb["lat"],
                            lon=adsb["lon"],
                            alt_m=(adsb.get("alt_baro", 0) or 0) * FT_TO_M,
                            vel_east=_gs_ms * math.sin(_trk),
                            vel_north=_gs_ms * math.cos(_trk),
                            vel_up=0.0,
                            rms_delay=0.0,
                            rms_doppler=0.0,
                            n_detections=event.get("length", 1),
                            timestamp_ms=event["timestamp"],
                            adsb_hex=adsb_hex,
                            latest_delay_us=None,
                            latest_doppler_hz=None,
                            target_class="aircraft",
                            is_anomalous=bool(_fb_anomaly_types),
                            anomaly_types=_fb_anomaly_types,
                            max_velocity_ms=max(_gs_ms, event.get("max_velocity_ms", 0.0)),
                        )
                        # Inherit pos_fix_ts from a shared entry when position
                        # hasn't moved — avoids starving dead-reckoning on the
                        # first frame when multiple nodes see the same aircraft.
                        _hex_key = existing.adsb_hex or existing.hex_id
                        with _state.geo_aircraft_lock:
                            _prev = _state.active_geo_aircraft.get(_hex_key)
                        if _prev is not None:
                            _pt = _prev[0]
                            if abs(_pt.lat - existing.lat) <= 1e-6 and abs(_pt.lon - existing.lon) <= 1e-6:
                                existing.pos_fix_ts = _pt.pos_fix_ts
                        self.geolocated_tracks[track_id] = existing
                    _hex_key = existing.adsb_hex or existing.hex_id
                    with _state.geo_aircraft_lock:
                        _state.active_geo_aircraft[_hex_key] = (existing, self.config)

    def process_frame(self, frame: dict):
        """Process a single detection frame {timestamp, delay[], doppler[], snr[], adsb?[]}."""
        ts = frame["timestamp"]
        delays = frame.get("delay", [])
        dopplers = frame.get("doppler", [])
        snrs = frame.get("snr", [])
        adsb_list = frame.get("adsb")  # aligned by index, may be None

        detections = []
        for i, (d, f, s) in enumerate(zip(delays, dopplers, snrs)):
            det = {"delay": d, "doppler": f, "snr": s}
            if adsb_list and i < len(adsb_list) and adsb_list[i] is not None:
                det["adsb"] = adsb_list[i]
            detections.append(det)

        # Feed to retina-tracker (Kalman + GNN)
        self.tracker.process_frame(detections, ts)

        # Run geolocation on updated track events
        self._run_geolocation()

        # Periodically prune stale track entries from per-track dicts
        self._frame_count += 1
        _now_mono = time.monotonic()
        if (_now_mono - self._last_prune_time) >= self._PRUNE_INTERVAL_S:
            self._last_prune_time = _now_mono
            self._prune_stale_tracks()

    def process_file(self, filepath: str) -> list:
        """Process an entire .detection file. Returns geolocated tracks."""
        with open(filepath, "r") as f:
            content = f.read().strip()
            if not content.startswith("["):
                content = "[" + content + "]"
            frames = json.loads(content)

        # Feed all frames to tracker only (skip per-frame geolocation for batch)
        for frame in frames:
            ts = frame["timestamp"]
            delays = frame.get("delay", [])
            dopplers = frame.get("doppler", [])
            snrs = frame.get("snr", [])
            detections = [
                {"delay": d, "doppler": f, "snr": s}
                for d, f, s in zip(delays, dopplers, snrs)
            ]
            self.tracker.process_frame(detections, ts)

        # Run geolocation once after all frames are processed
        self._run_geolocation()

        return list(self.geolocated_tracks.values())

    def generate_aircraft_json(self) -> dict:
        """Generate tar1090-compatible aircraft.json from geolocated tracks."""
        now = time.time()
        aircraft = []

        for track in self.geolocated_tracks.values():
            ac = {
                "hex": track.hex_id,
                "type": "tisb_other",
                "flight": f"PR{abs(hash(track.track_id)) % 10000:04d} ",
                "alt_baro": round(track.alt_ft),
                "alt_geom": round(track.alt_ft),
                "gs": round(track.speed_knots, 1),
                "track": round(track.track_angle, 1),
                "lat": round(track.lat, 6),
                "lon": round(track.lon, 6),
                "seen": 0,
                "seen_pos": 0,
                "messages": track.n_detections,
                "rssi": -10.0,
                "category": "A3",
            }
            if track.adsb_hex:
                ac["hex"] = track.adsb_hex
            aircraft.append(ac)

        return {
            "now": now,
            "messages": sum(t.n_detections for t in self.geolocated_tracks.values()),
            "aircraft": aircraft,
        }

    def generate_receiver_json(self) -> dict:
        """Generate tar1090-compatible receiver.json for the RX site."""
        return {
            "version": "retina-passive-radar",
            "refresh": 1000,
            "history": 0,
            "lat": self.config["rx_lat"],
            "lon": self.config["rx_lon"],
        }


def process_detection_folder(folder: str, output_dir: str, node_config: dict = None):
    """Process all .detection files in a folder and write tar1090 JSON to output_dir."""
    pipeline = PassiveRadarPipeline(node_config)

    detection_files = sorted(glob.glob(os.path.join(folder, "*.detection")))
    if not detection_files:
        logger.info("No .detection files found in %s", folder)
        return

    os.makedirs(output_dir, exist_ok=True)

    receiver = pipeline.generate_receiver_json()
    with open(os.path.join(output_dir, "receiver.json"), "w") as f:
        json.dump(receiver, f)

    for filepath in detection_files:
        logger.info("Processing: %s", os.path.basename(filepath))
        pipeline.process_file(filepath)

    aircraft_data = pipeline.generate_aircraft_json()
    with open(os.path.join(output_dir, "aircraft.json"), "w") as f:
        json.dump(aircraft_data, f)

    logger.info("Output: %d geolocated targets", len(aircraft_data['aircraft']))
    return aircraft_data


if __name__ == "__main__":
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    output = sys.argv[2] if len(sys.argv) > 2 else "./tar1090_data"
    process_detection_folder(folder, output)
