#!/usr/bin/env python3
"""
2D Range-Azimuth Levenberg-Marquardt solver for passive radar tracking.

Solves for target position and velocity using range-azimuth coordinates
with fixed altitude assumption. This addresses the geometric degeneracy
in single-node passive radar by reducing the problem to 2D.

State vector: [range, azimuth, v_radial, v_tangential]
- range: slant range from RX (km)
- azimuth: bearing from RX (degrees, 0-360)
- v_radial: radial velocity (positive = moving away from RX) (m/s)
- v_tangential: tangential velocity (positive = counterclockwise) (m/s)

Altitude is fixed (parameter, not optimized).
"""

import numpy as np
from scipy.optimize import least_squares

from .bistatic_models import bistatic_delay, bistatic_doppler
from .baseline_geometry import calculate_target_azimuth, antenna_gain_pattern


def state_to_cartesian_enu(range_km, azimuth_deg, altitude_m, rx_alt_m):
    """
    Convert range-azimuth-altitude to Cartesian ENU coordinates.

    Args:
        range_km: Slant range from RX (km)
        azimuth_deg: Azimuth from RX (degrees, 0=North, 90=East)
        altitude_m: Absolute altitude ASL (meters)
        rx_alt_m: Receiver altitude ASL (meters)

    Returns:
        (east, north, up) in km relative to RX
    """
    az_rad = np.radians(azimuth_deg)

    # Altitude relative to RX
    z_rel_m = altitude_m - rx_alt_m

    # Horizontal range (ground range)
    horiz_range_km = np.sqrt(range_km**2 - (z_rel_m/1000)**2) if range_km**2 > (z_rel_m/1000)**2 else range_km

    # Cartesian coordinates
    east = horiz_range_km * np.sin(az_rad)
    north = horiz_range_km * np.cos(az_rad)
    up = z_rel_m / 1000  # Convert to km

    return (east, north, up)


def velocities_to_cartesian(v_radial, v_tangential, azimuth_deg):
    """
    Convert radial/tangential velocities to Cartesian ENU.

    Args:
        v_radial: Radial velocity (m/s, positive = away from RX)
        v_tangential: Tangential velocity (m/s, positive = counterclockwise)
        azimuth_deg: Current azimuth (degrees)

    Returns:
        (ve, vn, vu) in m/s
    """
    az_rad = np.radians(azimuth_deg)

    # Velocity components
    ve = v_radial * np.sin(az_rad) + v_tangential * np.cos(az_rad)
    vn = v_radial * np.cos(az_rad) - v_tangential * np.sin(az_rad)
    vu = 0  # Fixed altitude assumption

    return (ve, vn, vu)


def residual_function_2d(state, track, tx_enu, rx_enu, frequency,
                         antenna_boresight, rx_alt_m, altitude_fixed_m):
    """
    Calculate residuals for 2D range-azimuth solver.

    Args:
        state: [range_km, azimuth_deg, v_radial_ms, v_tangential_ms]
        track: Track object with detections
        tx_enu: TX position in km
        rx_enu: RX position in km
        frequency: TX frequency in Hz
        antenna_boresight: Antenna boresight azimuth (degrees)
        rx_alt_m: Receiver altitude ASL (meters)
        altitude_fixed_m: Fixed target altitude ASL (meters)

    Returns:
        Array of residuals [delay, doppler, antenna, altitude] per detection
    """
    range_0, azimuth_0, v_radial, v_tangential = state

    # Reference time (first detection)
    t0 = track.detections[0].timestamp / 1000  # ms -> s

    residuals = []

    for det in track.detections:
        # Time since reference
        t = det.timestamp / 1000
        dt = t - t0

        # Update range and azimuth based on constant velocity
        # For small dt, this is approximately:
        # range(t) = range_0 + v_radial * dt / 1000 (convert m/s to km/s)
        # azimuth changes due to tangential velocity
        # d_azimuth = (v_tangential * dt) / (range * 1000) in radians, convert to degrees

        range_t = range_0 + (v_radial / 1000) * dt  # km

        # Azimuth rate (degrees/s) = v_tangential (m/s) / (range_t (km) * 1000 m/km) * (180/pi)
        if range_t > 0.1:  # Avoid division by zero
            azimuth_rate_deg_per_s = np.degrees(v_tangential / (range_t * 1000))
            azimuth_t = azimuth_0 + azimuth_rate_deg_per_s * dt
        else:
            azimuth_t = azimuth_0

        # Convert to Cartesian for bistatic calculations
        pos_enu = state_to_cartesian_enu(range_t, azimuth_t, altitude_fixed_m, rx_alt_m)
        vel_enu = velocities_to_cartesian(v_radial, v_tangential, azimuth_t)

        # Predict delay and Doppler
        delay_pred = bistatic_delay(pos_enu, tx_enu, rx_enu)
        doppler_pred = bistatic_doppler(pos_enu, vel_enu, tx_enu, rx_enu, frequency)

        # Residuals
        delay_res = det.delay - delay_pred  # μs
        doppler_res = det.doppler - doppler_pred  # Hz

        residuals.append(delay_res)
        residuals.append(doppler_res)

        # Antenna constraint (azimuth must be near boresight)
        gain = antenna_gain_pattern(azimuth_t, antenna_boresight, beamwidth_deg=48)
        antenna_res = (1.0 - gain) * 50.0
        residuals.append(antenna_res)

        # Altitude constraint (soft, prevents extreme altitudes)
        # This is implicit in fixed_altitude, but we add a small regularization
        alt_res = 0.0  # No residual needed since altitude is fixed
        residuals.append(alt_res)

    return np.array(residuals)


def solve_track_2d(track, initial_state, tx_enu, rx_enu, frequency,
                   antenna_boresight, rx_alt_m, altitude_fixed_m=1000):
    """
    Solve for track position and velocity using 2D range-azimuth optimization.

    Args:
        track: Track object with detections
        initial_state: [range_0, azimuth_0, v_radial, v_tangential]
        tx_enu: TX position in km
        rx_enu: RX position in km
        frequency: TX frequency in Hz
        antenna_boresight: Antenna boresight azimuth (degrees)
        rx_alt_m: Receiver altitude ASL (meters)
        altitude_fixed_m: Fixed target altitude ASL (meters, default 1000m)

    Returns:
        dict with solution and diagnostics
    """
    # Define bounds
    range_0, azimuth_0, v_radial_0, v_tangential_0 = initial_state

    bounds_lower = [
        0.5,                        # range > 0.5 km (minimum)
        antenna_boresight - 30,     # azimuth within ±30° of boresight (wider than beamwidth for robustness)
        -400,                       # v_radial (m/s)
        -400                        # v_tangential (m/s)
    ]

    bounds_upper = [
        150,                        # range < 150 km (maximum)
        antenna_boresight + 30,     # azimuth within ±30° of boresight
        400,                        # v_radial (m/s)
        400                         # v_tangential (m/s)
    ]

    # Run LM optimization
    result = least_squares(
        residual_function_2d,
        initial_state,
        args=(track, tx_enu, rx_enu, frequency, antenna_boresight, rx_alt_m, altitude_fixed_m),
        bounds=(bounds_lower, bounds_upper),
        method='trf',
        ftol=1e-8,
        xtol=1e-8,
        max_nfev=1000
    )

    # Extract results
    state_solution = result.x
    residuals = result.fun
    success = result.success

    # Calculate RMS errors (4 residuals per detection: delay, doppler, antenna, altitude)
    n_det = len(track.detections)
    delay_residuals = residuals[0::4]  # Every 4th starting from 0
    doppler_residuals = residuals[1::4]  # Every 4th starting from 1

    rms_delay = np.sqrt(np.mean(delay_residuals**2))
    rms_doppler = np.sqrt(np.mean(doppler_residuals**2))

    # Return solution
    return {
        'success': success,
        'state': state_solution,  # [range, azimuth, v_radial, v_tangential]
        'residuals': residuals,
        'rms_delay': rms_delay,
        'rms_doppler': rms_doppler,
        'cost': result.cost,
        'message': result.message,
        'nfev': result.nfev,
        'altitude_fixed': altitude_fixed_m
    }


if __name__ == "__main__":
    # Test with example data
    print("Testing 2D range-azimuth solver\n")

    # Mock track
    class MockDetection:
        def __init__(self, timestamp, delay, doppler, snr):
            self.timestamp = timestamp
            self.delay = delay
            self.doppler = doppler
            self.snr = snr

    class MockTrack:
        def __init__(self, detections):
            self.detections = detections

    # Create test detections (simulate aircraft at ~10km range, moving away)
    detections = []
    for i in range(20):
        t = 1000 * i  # ms
        delay = 30 + 0.1 * i  # μs, increasing (moving away)
        doppler = 10 - 0.2 * i  # Hz, decreasing
        detections.append(MockDetection(t, delay, doppler, 10))

    track = MockTrack(detections)

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

    # Initial guess
    initial_state = [10, geometry['antenna_boresight'], 50, 0]  # 10km range, on boresight, 50 m/s radial, 0 tangential

    # Solve
    result = solve_track_2d(track, initial_state, tx_enu, (0,0,0), config.frequency,
                           geometry['antenna_boresight'], config.rx_alt, altitude_fixed_m=1000)

    print(f"Success: {result['success']}")
    print(f"Solution: range={result['state'][0]:.2f} km, azimuth={result['state'][1]:.1f}°, v_r={result['state'][2]:.1f} m/s, v_t={result['state'][3]:.1f} m/s")
    print(f"RMS delay: {result['rms_delay']:.3f} μs")
    print(f"RMS Doppler: {result['rms_doppler']:.3f} Hz")
    print(f"Iterations: {result['nfev']}")
