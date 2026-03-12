"""
Configuration and data loader for single-node passive radar.
Loads radar system config (config.yml) and geolocator config (geolocator_config.yml).
"""

import yaml
import json
import math
from pathlib import Path


def validate_adsb_data(adsb):
    """
    Validate ADS-B data fields are within reasonable ranges.

    Args:
        adsb: Dictionary with ADS-B fields (lat, lon, alt_baro, gs, track)

    Returns:
        bool: True if valid, False if invalid or missing required fields
    """
    if not isinstance(adsb, dict):
        return False

    # Check required fields
    if 'lat' not in adsb or 'lon' not in adsb:
        return False

    # Validate latitude
    lat = adsb['lat']
    if not isinstance(lat, (int, float)) or math.isnan(lat) or not (-90 <= lat <= 90):
        return False

    # Validate longitude
    lon = adsb['lon']
    if not isinstance(lon, (int, float)) or math.isnan(lon) or not (-180 <= lon <= 180):
        return False

    # Validate altitude (optional but common)
    if 'alt_baro' in adsb:
        alt = adsb['alt_baro']
        if not isinstance(alt, (int, float)) or math.isnan(alt) or not (-2000 <= alt <= 50000):
            return False

    # Validate ground speed (optional)
    if 'gs' in adsb:
        gs = adsb['gs']
        if not isinstance(gs, (int, float)) or math.isnan(gs) or not (0 <= gs <= 1000):
            return False

    # Validate track angle (optional)
    if 'track' in adsb:
        track = adsb['track']
        if not isinstance(track, (int, float)) or math.isnan(track) or not (0 <= track < 360):
            return False

    return True


class GeolocatorConfig:
    """Geolocator-specific configuration."""

    def __init__(self, config_dict):
        solver = config_dict.get('solver', {})
        self.beamwidth_deg = solver.get('beamwidth_deg', 48)
        self.altitude_bounds = solver.get('altitude_bounds', [50, 15000])
        self.velocity_bounds = solver.get('velocity_bounds', [-400, 400])
        self.min_detections = solver.get('min_detections', 3)
        self.max_detections = solver.get('max_detections', 20)
        self.temporal_continuity = solver.get('temporal_continuity', True)
        self.initial_altitude_m = solver.get('initial_altitude_m', 1000)

        self.use_adsb_initial_guess = solver.get('use_adsb_initial_guess', True)
        self.adsb_fallback_to_geometric = solver.get('adsb_fallback_to_geometric', True)
        self.validate_against_adsb = solver.get('validate_against_adsb', True)

        self._validate_adsb_config()

        self.ftol = solver.get('ftol', 1.0e-8)
        self.xtol = solver.get('xtol', 1.0e-8)
        self.max_iterations = solver.get('max_iterations', 1000)
        self.max_rms_delay_us = solver.get('max_rms_delay_us', 2.0)
        self.max_rms_doppler_hz = solver.get('max_rms_doppler_hz', 2.0)

        radar_cfg = config_dict.get('radar_config', {})
        self.primary_config_path = radar_cfg.get('primary')
        self.fallback_config_path = radar_cfg.get('fallback')

        output = config_dict.get('output', {})
        self.output_format = output.get('format', 'jsonl')
        self.output_directory = output.get('directory', 'data/output')
        self.verbose = output.get('verbose', True)

        plotting = config_dict.get('plotting', {})
        self.plotting_enabled = plotting.get('enabled', False)
        self.plot_output_dir = plotting.get('output_dir', 'data/plots')
        self.plot_dpi = plotting.get('dpi', 150)

    def _validate_adsb_config(self):
        """Validate ADS-B configuration values."""
        if not isinstance(self.use_adsb_initial_guess, bool):
            raise ValueError(
                f"use_adsb_initial_guess must be boolean, got {type(self.use_adsb_initial_guess).__name__}"
            )

        if not isinstance(self.adsb_fallback_to_geometric, bool):
            raise ValueError(
                f"adsb_fallback_to_geometric must be boolean, got {type(self.adsb_fallback_to_geometric).__name__}"
            )

        if not isinstance(self.validate_against_adsb, bool):
            raise ValueError(
                f"validate_against_adsb must be boolean, got {type(self.validate_against_adsb).__name__}"
            )

    def __repr__(self):
        return (f"GeolocatorConfig(beamwidth={self.beamwidth_deg}°, "
                f"min_det={self.min_detections}, temporal={self.temporal_continuity}, "
                f"adsb={self.use_adsb_initial_guess})")


def load_geolocator_config(config_path='geolocator_config.yml'):
    """
    Load geolocator-specific configuration.

    Args:
        config_path: Path to geolocator_config.yml

    Returns:
        GeolocatorConfig object
    """
    with open(config_path, 'r') as f:
        config_dict = yaml.safe_load(f)
    return GeolocatorConfig(config_dict)


class Detection:
    """Single detection with timestamp, delay, Doppler, and SNR."""

    def __init__(self, timestamp, delay, doppler, snr, adsb=None):
        self.timestamp = timestamp  # Unix milliseconds
        self.delay = delay  # microseconds
        self.doppler = doppler  # Hz
        self.snr = snr  # dB
        self.adsb = adsb  # Optional ADS-B dict (lat, lon, alt_baro, gs, track)

    def __repr__(self):
        base = f"Detection(t={self.timestamp}, delay={self.delay:.2f}μs, doppler={self.doppler:.2f}Hz, snr={self.snr:.1f}dB"
        if self.adsb is not None:
            base += ", adsb=✓"
        base += ")"
        return base


class Track:
    """Track with ID, detections, and metadata."""

    def __init__(self, track_id, detections, event_data=None):
        self.track_id = track_id
        self.detections = detections  # List of Detection objects
        self.event_data = event_data  # Full event dict if needed

        # Parse ADS-B metadata from event_data
        self.adsb_hex = None
        self.adsb_initialized = False

        if event_data is not None:
            self.adsb_hex = event_data.get('adsb_hex')
            self.adsb_initialized = event_data.get('adsb_initialized', False)

    def __len__(self):
        return len(self.detections)

    def __repr__(self):
        if len(self.detections) > 0:
            t_start = self.detections[0].timestamp
            t_end = self.detections[-1].timestamp
            duration = (t_end - t_start) / 1000  # seconds
            base = f"Track(id={self.track_id}, N={len(self)}, duration={duration:.1f}s"
            if self.adsb_initialized:
                base += f", adsb={self.adsb_hex}"
            base += ")"
            return base
        return f"Track(id={self.track_id}, N=0)"


class Config:
    """System configuration from config.yml."""

    def __init__(self, config_dict):
        # Extract receiver position
        location = config_dict.get('location', {})
        rx_info = location.get('rx', {})
        self.rx_lat = rx_info.get('latitude')
        self.rx_lon = rx_info.get('longitude')
        self.rx_alt = rx_info.get('altitude')
        self.rx_name = rx_info.get('name', 'RX')
        self.rx_lla = (self.rx_lat, self.rx_lon, self.rx_alt)

        # Extract transmitter position
        tx_info = location.get('tx', {})
        if tx_info:
            self.tx_lat = tx_info.get('latitude')
            self.tx_lon = tx_info.get('longitude')
            self.tx_alt = tx_info.get('altitude')
            self.tx_name = tx_info.get('name', 'TX')
            self.tx_lla = (self.tx_lat, self.tx_lon, self.tx_alt)
        else:
            raise ValueError("No transmitter found in config")

        # Extract radio configuration
        capture = config_dict.get('capture', {})
        self.frequency = capture.get('fc')  # Hz

        # Extract processing parameters
        process = config_dict.get('process', {})
        data = process.get('data', {})
        self.cpi = data.get('cpi')  # seconds

    def __repr__(self):
        return (f"Config(RX=({self.rx_lat:.5f}, {self.rx_lon:.5f}, {self.rx_alt}m), "
                f"TX=({self.tx_lat:.5f}, {self.tx_lon:.5f}, {self.tx_alt}m, '{self.tx_name}'), "
                f"freq={self.frequency/1e6:.1f}MHz)")


def load_config(config_path=None, primary_path=None, fallback_path=None):
    """
    Load radar system configuration from YAML file with fallback support.

    Args:
        config_path: Direct path to config.yml (overrides primary/fallback)
        primary_path: Primary config path (e.g., /opt/blah2/config/config.yml)
        fallback_path: Fallback config path (e.g., data/config/radar_config.yml)

    Returns:
        Config object

    Raises:
        FileNotFoundError: If no valid config file found
    """
    # Direct path specified - use it
    if config_path is not None:
        with open(config_path, 'r') as f:
            config_dict = yaml.safe_load(f)
        return Config(config_dict)

    # Try primary path first
    if primary_path is not None:
        primary = Path(primary_path)
        if primary.exists():
            with open(primary, 'r') as f:
                config_dict = yaml.safe_load(f)
            return Config(config_dict)

    # Try fallback path
    if fallback_path is not None:
        fallback = Path(fallback_path)
        if fallback.exists():
            with open(fallback, 'r') as f:
                config_dict = yaml.safe_load(f)
            return Config(config_dict)

    # No config found
    error_msg = "No radar system configuration file found.\n"
    if primary_path:
        error_msg += f"  Primary: {primary_path} (not found)\n"
    if fallback_path:
        error_msg += f"  Fallback: {fallback_path} (not found)\n"
    error_msg += "\nPlease copy your system config.yml to data/config/radar_config.yml or update geolocator_config.yml"
    raise FileNotFoundError(error_msg)


def load_tracks(jsonl_path, min_detections=10):
    """
    Load tracks from JSONL file.

    Args:
        jsonl_path: Path to events_full_window.jsonl
        min_detections: Minimum number of detections required for a track

    Returns:
        List of Track objects (filtered by min_detections)
    """
    tracks = []

    with open(jsonl_path, 'r') as f:
        for line in f:
            event = json.loads(line)

            # Extract track information
            track_id = event.get('track_id')
            n_total = event.get('n_total', 0)

            # Filter by minimum detections
            if n_total < min_detections:
                continue

            # Parse detections
            detections = []
            for det_dict in event.get('detections', []):
                # Parse ADS-B data if present
                adsb = None
                if 'adsb' in det_dict:
                    adsb_data = det_dict['adsb']
                    # Validate ADS-B data before using it
                    if validate_adsb_data(adsb_data):
                        adsb = adsb_data
                    # If validation fails, adsb remains None (silently ignore invalid data)

                det = Detection(
                    timestamp=det_dict['timestamp'],
                    delay=det_dict['delay'],
                    doppler=det_dict['doppler'],
                    snr=det_dict['snr'],
                    adsb=adsb  # Pass validated ADS-B or None
                )
                detections.append(det)

            # Create track
            track = Track(track_id, detections, event)
            tracks.append(track)

    return tracks


def delay_to_range(delay_us):
    """
    Convert bistatic delay in microseconds to range in km.

    Args:
        delay_us: Delay in microseconds

    Returns:
        Range in km (total path TX→Target→RX)
    """
    # Speed of light: 300 km/ms = 0.3 km/μs
    # Delay is two-way time, so range = delay * c
    return delay_us * 0.3


if __name__ == "__main__":
    # Test loading
    import os
    script_dir = Path(__file__).parent

    config_path = script_dir / "config.yml"
    jsonl_path = script_dir / "events_full_window.jsonl"

    print("Loading config...")
    config = load_config(config_path)
    print(config)
    print()

    print("Loading tracks (min 10 detections)...")
    tracks = load_tracks(jsonl_path, min_detections=10)
    print(f"Loaded {len(tracks)} tracks")
    print()

    # Show first few tracks
    print("First 5 tracks:")
    for i, track in enumerate(tracks[:5]):
        print(f"  {i+1}. {track}")
        print(f"      First detection: {track.detections[0]}")
        print(f"      Last detection:  {track.detections[-1]}")
    print()

    # Show delay→range conversion
    print("Delay to range conversion:")
    for delay in [10, 20, 30, 40, 50]:
        range_km = delay_to_range(delay)
        print(f"  {delay} μs → {range_km:.1f} km")
