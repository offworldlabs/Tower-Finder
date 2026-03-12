#!/usr/bin/env python3
"""
Generate initial guess for 2D range-azimuth solver.

Uses bistatic delay and antenna boresight to estimate
initial range and azimuth.
"""

import numpy as np


def generate_initial_guess_2d(track, tx_enu, antenna_boresight, frequency, altitude_fixed_m=1000):
    """
    Generate initial guess for 2D range-azimuth solver.

    Args:
        track: Track object with detections
        tx_enu: TX position in ENU coordinates (km)
        antenna_boresight: Antenna boresight azimuth (degrees)
        frequency: TX frequency (Hz)
        altitude_fixed_m: Fixed altitude ASL (meters)

    Returns:
        [range_km, azimuth_deg, v_radial_ms, v_tangential_ms]
    """
    # Use first detection for initial estimates
    first_det = track.detections[0]
    delay = first_det.delay  # μs
    doppler = first_det.doppler  # Hz

    # Estimate bistatic range from delay
    # delay = (bistatic_range - baseline_distance) / c
    # bistatic_range ≈ delay * c + baseline_distance
    c_km_per_us = 0.3  # km/μs
    baseline_distance = np.linalg.norm(tx_enu)  # km

    bistatic_range = delay * c_km_per_us + baseline_distance  # km

    # For single-node passive radar with target near boresight:
    # Rough approximation: range_to_target ≈ bistatic_range / 2
    # (This assumes target is roughly halfway between TX and RX along the bistatic angle)
    # More accurate estimate requires iterative calculation

    # Better estimate: Use TX-RX geometry
    # If target is at azimuth = boresight and range = r_rx from RX:
    # bistatic_range = r_tx_to_target + r_rx_to_target
    # We need to solve for r_rx_to_target

    # Simple approximation for now
    range_est = bistatic_range / 2.5  # Empirical factor

    # Ensure minimum range
    if range_est < 1.0:
        range_est = 5.0  # Default to 5 km

    # Azimuth: start at boresight
    azimuth_est = antenna_boresight

    # Estimate radial velocity from Doppler
    # Bistatic Doppler: f_d = (f_tx / c) * (v·û_tx + v·û_rx)
    # For single-node, if target is on boresight and moving radially:
    # Rough approximation: v_radial ≈ doppler * c / (2 * frequency)

    c_m_per_s = 299792458  # m/s
    v_radial_est = (doppler * c_m_per_s) / (2 * frequency)  # m/s

    # Tangential velocity: start at zero
    v_tangential_est = 0.0

    return [range_est, azimuth_est, v_radial_est, v_tangential_est]


def generate_initial_guess_2d_from_previous(prev_solution, track, dt_seconds):
    """
    Generate initial guess from previous solution using temporal continuity.

    Args:
        prev_solution: Previous solution dict with 'state' = [range, az, v_r, v_t]
        track: Current track object
        dt_seconds: Time elapsed since previous solution (seconds)

    Returns:
        [range_km, azimuth_deg, v_radial_ms, v_tangential_ms]
    """
    range_prev, azimuth_prev, v_radial_prev, v_tangential_prev = prev_solution['state']

    # Extrapolate range
    range_new = range_prev + (v_radial_prev / 1000) * dt_seconds  # km

    # Extrapolate azimuth
    # azimuth_rate = v_tangential / range (rad/s)
    if range_prev > 0.1:
        azimuth_rate_rad_per_s = v_tangential_prev / (range_prev * 1000)
        azimuth_new = azimuth_prev + np.degrees(azimuth_rate_rad_per_s) * dt_seconds
    else:
        azimuth_new = azimuth_prev

    # Velocities: assume constant
    v_radial_new = v_radial_prev
    v_tangential_new = v_tangential_prev

    return [range_new, azimuth_new, v_radial_new, v_tangential_new]


if __name__ == "__main__":
    # Test initial guess generation
    print("Testing 2D initial guess generation\n")

    class MockDetection:
        def __init__(self, timestamp, delay, doppler):
            self.timestamp = timestamp
            self.delay = delay
            self.doppler = doppler

    class MockTrack:
        def __init__(self, detections):
            self.detections = detections

    # Test detection (10 km range, moderate delay/doppler)
    det = MockDetection(1000, 25.0, 15.0)
    track = MockTrack([det])

    # Geometry
    import sys
    sys.path.append('.')
    from config_loader import load_config
    from baseline_geometry import calculate_baseline_geometry
    from Geometry import Geometry

    config = load_config("config.yml")
    geometry = calculate_baseline_geometry(config.rx_lla, config.tx_lla)

    tx_ecef = Geometry.lla2ecef(config.tx_lla[0], config.tx_lla[1], config.tx_lla[2])
    tx_enu_m = Geometry.ecef2enu(tx_ecef[0], tx_ecef[1], tx_ecef[2],
                                   config.rx_lla[0], config.rx_lla[1], config.rx_lla[2])
    tx_enu = tuple(x / 1000 for x in tx_enu_m)

    # Generate initial guess
    guess = generate_initial_guess_2d(track, tx_enu, geometry['antenna_boresight'],
                                       config.frequency, altitude_fixed_m=1000)

    print(f"Initial guess:")
    print(f"  Range: {guess[0]:.2f} km")
    print(f"  Azimuth: {guess[1]:.1f}°")
    print(f"  Radial velocity: {guess[2]:.1f} m/s")
    print(f"  Tangential velocity: {guess[3]:.1f} m/s")
    print()

    # Test temporal continuity
    print("Testing temporal continuity:")
    prev_solution = {'state': guess}
    dt = 10.0  # 10 seconds elapsed
    guess_new = generate_initial_guess_2d_from_previous(prev_solution, track, dt)

    print(f"  New guess after {dt}s:")
    print(f"    Range: {guess_new[0]:.2f} km")
    print(f"    Azimuth: {guess_new[1]:.1f}°")
