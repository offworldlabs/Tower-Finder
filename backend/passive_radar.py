"""
Passive Radar Detection Pipeline
Uses retina-tracker (Kalman+GNN) and retina-geolocator (LM solver)
to process detection data and output tar1090-compatible aircraft.json.

Pipeline: detection data → retina-tracker → retina-geolocator → tar1090 JSON
"""

import json
import math
import os
import time
import glob
import io
from pathlib import Path
from typing import Optional

import yaml

import numpy as np

from retina_tracker.tracker import Tracker as RetinaTracker
from retina_tracker.output import TrackEventWriter

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
C = 299_792_458.0  # speed of light m/s
FT_TO_M = 0.3048

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
        self.events = {}  # track_id → latest event dict

    def write_event(self, track_id, timestamp, length, detections,
                    adsb_hex=None, adsb_initialized=False,
                    is_anomalous=False, max_velocity_ms=0.0):
        self.events[track_id] = {
            "track_id": track_id,
            "timestamp": timestamp,
            "length": length,
            "detections": detections,
            "adsb_hex": adsb_hex,
            "adsb_initialized": adsb_initialized,
            "is_anomalous": is_anomalous,
            "max_velocity_ms": max_velocity_ms,
        }

    def get_events(self):
        return self.events


def _enu_to_lla(enu_km, rx_lat, rx_lon, rx_alt):
    """Convert ENU position (km) to LLA coordinates."""
    enu_m = (enu_km[0] * 1000, enu_km[1] * 1000, enu_km[2] * 1000)
    ecef = Geometry.enu2ecef(enu_m[0], enu_m[1], enu_m[2], rx_lat, rx_lon, rx_alt)
    return Geometry.ecef2lla(ecef[0], ecef[1], ecef[2])


class GeolocatedTrack:
    """A track that has been geolocated by the LM solver."""

    def __init__(self, track_id, lat, lon, alt_m, vel_east, vel_north, vel_up,
                 rms_delay, rms_doppler, n_detections, timestamp_ms,
                 adsb_hex=None):
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
        self.adsb_hex = adsb_hex

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

    def __init__(self, node_config: dict = None):
        config = node_config or DEFAULT_NODE_CONFIG
        self.config = config
        self.node_id = config.get("node_id", "net13")

        # Set up retina-tracker with in-memory event writer
        self.event_writer = InMemoryEventWriter()

        # Load tracker config
        tracker_config_path = os.path.join(
            os.path.dirname(__file__), "retina_tracker", "config.yaml"
        )
        tracker_config = {}
        if os.path.exists(tracker_config_path):
            with open(tracker_config_path, "r") as f:
                tracker_config = yaml.safe_load(f)

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

        # Load geolocator config
        geo_config_path = os.path.join(
            os.path.dirname(__file__), "retina_geolocator", "geolocator_config.yml"
        )
        if os.path.exists(geo_config_path):
            self.geo_config = load_geolocator_config(geo_config_path)
        else:
            self.geo_config = None

    def _geolocate_track_event(self, track_id, event):
        """Run LM solver on a track event to get lat/lon/alt/velocity."""
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

        # Solve
        result = solve_track(
            geo_track,
            initial_guess,
            self.tx_enu,
            self.rx_enu,
            self.frequency,
            self.geometry["antenna_boresight"],
            self.rx_lla[2],  # rx_alt_m
        )

        if not result["success"]:
            return None

        # Store solution for temporal continuity
        self._previous_solutions[track_id] = result["state"]

        # Convert ENU (km) → LLA
        final_enu_km = result["state"][:3]
        lat, lon, alt = _enu_to_lla(final_enu_km, *self.rx_lla)

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
        )

    def _run_geolocation(self):
        """Run geolocation on all track events from the event writer."""
        for track_id, event in self.event_writer.get_events().items():
            result = self._geolocate_track_event(track_id, event)
            if result is not None:
                self.geolocated_tracks[track_id] = result

    def process_frame(self, frame: dict):
        """Process a single detection frame {timestamp, delay[], doppler[], snr[]}."""
        ts = frame["timestamp"]
        delays = frame.get("delay", [])
        dopplers = frame.get("doppler", [])
        snrs = frame.get("snr", [])

        detections = [
            {"delay": d, "doppler": f, "snr": s}
            for d, f, s in zip(delays, dopplers, snrs)
        ]

        # Feed to retina-tracker (Kalman + GNN)
        self.tracker.process_frame(detections, ts)

        # Run geolocation on updated track events
        self._run_geolocation()

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
        print(f"No .detection files found in {folder}")
        return

    os.makedirs(output_dir, exist_ok=True)

    receiver = pipeline.generate_receiver_json()
    with open(os.path.join(output_dir, "receiver.json"), "w") as f:
        json.dump(receiver, f)

    for filepath in detection_files:
        print(f"Processing: {os.path.basename(filepath)}")
        pipeline.process_file(filepath)

    aircraft_data = pipeline.generate_aircraft_json()
    with open(os.path.join(output_dir, "aircraft.json"), "w") as f:
        json.dump(aircraft_data, f)

    print(f"Output: {len(aircraft_data['aircraft'])} geolocated targets")
    return aircraft_data


if __name__ == "__main__":
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    output = sys.argv[2] if len(sys.argv) > 2 else "./tar1090_data"
    process_detection_folder(folder, output)
