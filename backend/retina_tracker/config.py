"""Configuration loading and defaults for retina-tracker."""

import os
import sys

import yaml


def load_blah2_config(blah2_config_path):
    """Load center frequency from blah2 config file.

    Args:
        blah2_config_path: Path to blah2 config.yml file

    Returns:
        Center frequency in Hz, or None if not found
    """
    try:
        with open(blah2_config_path, "r") as f:
            config = yaml.safe_load(f)
        fc = config.get("capture", {}).get("fc")
        if fc is not None:
            print(
                f"Loaded center frequency {fc / 1e6:.1f} MHz from {blah2_config_path}",
                file=sys.stderr,
            )
        return fc
    except (OSError, yaml.YAMLError) as e:
        print(
            f"Warning: Failed to load blah2 config from {blah2_config_path}: {e}",
            file=sys.stderr,
        )
        return None


def load_config(config_path=None):
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, looks for config.yaml in:
                    1. Current directory
                    2. Parent directory (for running from tracker/)

    Returns:
        Dict with configuration values, or defaults if no config found.
    """
    default_config = {
        "mode": "node",
        "tracker": {
            "m_threshold": 4,
            "n_window": 6,
            "n_delete": 10,
            "min_snr": 7.0,
            "gate_threshold": 2.0,
            "detection_window": 20,
        },
        "process_noise": {"delay": 0.1, "doppler": 0.5},
        "tracklet": {"max_delay_residual": 2.0, "max_doppler_residual": 10.0, "max_time_span": 3.0},
        "adsb": {
            "enabled": False,
            "priority": True,
            "reference_location": None,
            "initial_covariance": {"position": 100.0, "velocity": 5.0},
        },
        "radar": {
            "blah2_config": None,
            "center_frequency": 200000000,
        },
        "tcp": {
            "host": "0.0.0.0",
            "port": 3012,
        },
    }

    if config_path is None:
        search_paths = ["config.yaml", "../config.yaml"]
        for path in search_paths:
            if os.path.exists(path):
                config_path = path
                break

    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            loaded = yaml.safe_load(f)
            for key in default_config:
                if key in loaded:
                    default_config[key].update(loaded[key])
        print(f"Loaded config from {config_path}", file=sys.stderr)

    return default_config


# Global config (loaded at module import or via set_config)
_config = None


def get_config():
    """Get current configuration, loading defaults if needed."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config):
    """Set configuration dict."""
    global _config
    _config = config


def _get_param(section, key, default=None):
    """Get parameter from config."""
    config = get_config()
    return config.get(section, {}).get(key, default)


# Track confirmation (M-of-N logic)
def M_THRESHOLD():
    return _get_param("tracker", "m_threshold", 4)


def N_WINDOW():
    return _get_param("tracker", "n_window", 6)


def N_DELETE():
    return _get_param("tracker", "n_delete", 10)


N_COAST = 3


def GATE_THRESHOLD():
    return _get_param("tracker", "gate_threshold", 2.0)


def MIN_SNR():
    return _get_param("tracker", "min_snr", 7.0)


def PROCESS_NOISE_DELAY():
    return _get_param("process_noise", "delay", 0.1)


def PROCESS_NOISE_DOPPLER():
    return _get_param("process_noise", "doppler", 0.5)


MEASUREMENT_NOISE_DELAY = 1.0
MEASUREMENT_NOISE_DOPPLER = 5.0


def TRACKLET_MAX_DELAY_RESIDUAL():
    return _get_param("tracklet", "max_delay_residual", 2.0)


def TRACKLET_MAX_DOPPLER_RESIDUAL():
    return _get_param("tracklet", "max_doppler_residual", 10.0)


def TRACKLET_MAX_TIME_SPAN():
    return _get_param("tracklet", "max_time_span", 3.0)


# Anomaly detection constants
SPEED_OF_LIGHT = 299792458.0
MACH_1_MS = 343.0
KNOTS_TO_MS = 0.514444
MAX_NORMAL_ACCEL_MS2 = 15.0
MAX_DIRECTION_CHANGE_DEG_PER_SEC = 30.0


def get_mach1_doppler_threshold():
    """Calculate Doppler threshold for Mach 1 based on center frequency.

    Uses worst-case bistatic geometry (TX/RX collocated): f_d = 2 * v * fc / c
    """
    fc = _get_param("radar", "center_frequency", 200000000)
    return 2 * MACH_1_MS * fc / SPEED_OF_LIGHT
