#!/usr/bin/env python3
"""
Radar tracker for bistatic radar detection data from blah2.

This module re-exports from the split submodules for backward compatibility.
New code should import directly from:
  tracker.config, tracker.kalman, tracker.track, tracker.tracker,
  tracker.output, tracker.server, tracker.cli
"""

# Re-export everything that was previously importable from this module.

# Configuration
from .config import (
    KNOTS_TO_MS,
    MACH_1_MS,
    MAX_DIRECTION_CHANGE_DEG_PER_SEC,
    MAX_NORMAL_ACCEL_MS2,
    MEASUREMENT_NOISE_DELAY,
    MEASUREMENT_NOISE_DOPPLER,
    N_COAST,
    SPEED_OF_LIGHT,
    GATE_THRESHOLD,
    M_THRESHOLD,
    MIN_SNR,
    N_DELETE,
    N_WINDOW,
    PROCESS_NOISE_DELAY,
    PROCESS_NOISE_DOPPLER,
    TRACKLET_MAX_DELAY_RESIDUAL,
    TRACKLET_MAX_DOPPLER_RESIDUAL,
    TRACKLET_MAX_TIME_SPAN,
    get_config,
    get_mach1_doppler_threshold,
    load_blah2_config,
    load_config,
    set_config,
)

# Kalman filter
from .kalman import KalmanFilter

# Track
from .track import Track, TrackState

# Tracker
from .tracker import Tracker

# Output
from .output import TrackEventWriter

# Server / streaming
from .server import process_streaming_frame, run_tcp_server

# CLI / file processing
from .cli import (
    load_detections,
    main,
    process_detections,
    save_tracks,
    visualize_tracks,
)

__all__ = [
    # Config
    "load_config",
    "load_blah2_config",
    "get_config",
    "set_config",
    "M_THRESHOLD",
    "N_WINDOW",
    "N_DELETE",
    "N_COAST",
    "GATE_THRESHOLD",
    "MIN_SNR",
    "PROCESS_NOISE_DELAY",
    "PROCESS_NOISE_DOPPLER",
    "MEASUREMENT_NOISE_DELAY",
    "MEASUREMENT_NOISE_DOPPLER",
    "TRACKLET_MAX_DELAY_RESIDUAL",
    "TRACKLET_MAX_DOPPLER_RESIDUAL",
    "TRACKLET_MAX_TIME_SPAN",
    "SPEED_OF_LIGHT",
    "MACH_1_MS",
    "KNOTS_TO_MS",
    "MAX_NORMAL_ACCEL_MS2",
    "MAX_DIRECTION_CHANGE_DEG_PER_SEC",
    "get_mach1_doppler_threshold",
    # Core classes
    "KalmanFilter",
    "TrackState",
    "Track",
    "TrackEventWriter",
    "Tracker",
    # Processing
    "load_detections",
    "process_detections",
    "save_tracks",
    "process_streaming_frame",
    "run_tcp_server",
    "visualize_tracks",
    "main",
]


if __name__ == "__main__":
    main()
