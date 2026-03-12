"""
Levenberg-Marquardt solver for track-based geolocation.
Fits constant velocity model to time series of bistatic detections.
"""

import numpy as np
from scipy.optimize import least_squares
from .bistatic_models import bistatic_delay, bistatic_doppler
from .baseline_geometry import calculate_target_azimuth, antenna_gain_pattern


def residual_function(state, track, tx_enu, rx_enu, frequency, antenna_boresight=None, rx_alt_m=0):
    """
    Calculate residuals for all detections in a track.

    Args:
        state: [x0, y0, z0, vx, vy, vz] - position (km) and velocity (m/s) at t0
        track: Track object with detections
        tx_enu: TX position in km
        rx_enu: RX position in km
        frequency: TX frequency in Hz
        antenna_boresight: Antenna boresight azimuth in degrees (None to disable constraint)
        rx_alt_m: Receiver altitude in meters ASL (for altitude constraint)

    Returns:
        residuals: Array of [delay_res_1, doppler_res_1, antenna_res_1, altitude_res_1, ...]
    """
    x0, y0, z0, vx, vy, vz = state

    # Reference time (first detection)
    t0 = track.detections[0].timestamp / 1000  # Convert ms to seconds

    residuals = []

    for det in track.detections:
        # Time since reference
        t = det.timestamp / 1000  # Convert ms to seconds
        dt = t - t0  # seconds

        # Predict position using constant velocity model
        # position(t) = position(t0) + velocity * dt
        # velocity is in m/s, need to convert to km/s for position update
        pos = np.array([
            x0 + (vx / 1000) * dt,  # km
            y0 + (vy / 1000) * dt,  # km
            z0 + (vz / 1000) * dt   # km
        ])

        # Velocity is constant
        vel = np.array([vx, vy, vz])  # m/s

        # Predict delay and Doppler
        delay_pred = bistatic_delay(pos, tx_enu, rx_enu)
        doppler_pred = bistatic_doppler(pos, vel, tx_enu, rx_enu, frequency)

        # Calculate residuals
        delay_res = det.delay - delay_pred  # μs
        doppler_res = det.doppler - doppler_pred  # Hz

        residuals.append(delay_res)
        residuals.append(doppler_res)

        # Add antenna constraint if boresight provided
        if antenna_boresight is not None:
            # Calculate target azimuth from receiver
            target_azimuth = calculate_target_azimuth(pos)

            # Calculate antenna gain (0-1, where 1 is on boresight)
            gain = antenna_gain_pattern(target_azimuth, antenna_boresight, beamwidth_deg=48)

            # Antenna constraint residual (penalize low gain = off-boresight)
            # Weight heavily to enforce beam constraint
            # Residual = 0 when on boresight, large when off-boresight
            antenna_res = (1.0 - gain) * 50.0  # Scale to match delay/Doppler magnitude
            residuals.append(antenna_res)

        # Add altitude constraint (prevent negative ASL altitude)
        # ENU z is relative to receiver, so absolute altitude = rx_alt + z_enu * 1000
        altitude_asl_m = rx_alt_m + pos[2] * 1000  # Convert z from km to m
        min_alt_asl_m = 50  # Minimum 50m above sea level

        if altitude_asl_m < min_alt_asl_m:
            # Penalize negative altitude heavily
            altitude_res = (min_alt_asl_m - altitude_asl_m) * 0.1  # Weight to match delay/Doppler scale
            residuals.append(altitude_res)
        else:
            # No penalty when above minimum
            residuals.append(0.0)

    return np.array(residuals)


def solve_track(track, initial_state, tx_enu, rx_enu, frequency, antenna_boresight=None, rx_alt_m=0):
    """
    Solve for track position and velocity using LM optimization.

    Args:
        track: Track object with detections
        initial_state: [x0, y0, z0, vx, vy, vz] initial guess
        tx_enu: TX position in km
        rx_enu: RX position in km
        frequency: TX frequency in Hz
        antenna_boresight: Antenna boresight azimuth in degrees (None to disable constraint)
        rx_alt_m: Receiver altitude in meters ASL (for proper altitude bounds)

    Returns:
        dict with:
            - success: bool, whether solver converged
            - state: [x0, y0, z0, vx, vy, vz] solution
            - residuals: final residual array
            - rms_delay: RMS delay error in μs
            - rms_doppler: RMS Doppler error in Hz
            - cost: final cost function value
            - message: solver message
    """
    # Define bounds
    # Position: reasonable range around initial guess
    pos_range = 50  # km
    x0, y0, z0, vx0, vy0, vz0 = initial_state

    # Altitude bound: prevent negative ASL altitude
    # ENU z is relative to receiver, so minimum z = -rx_alt to get 0m ASL
    # Add small margin (50m) to stay safely above ground
    min_alt_asl_m = 50  # minimum 50m above sea level
    min_z_enu_km = -(rx_alt_m - min_alt_asl_m) / 1000  # Convert to km

    bounds_lower = [
        x0 - pos_range,  # x
        y0 - pos_range,  # y
        min_z_enu_km,     # z (ENU up, minimum ASL altitude ~50m)
        -300,             # vx (m/s)
        -300,             # vy (m/s)
        -100              # vz (m/s)
    ]

    bounds_upper = [
        x0 + pos_range,  # x
        y0 + pos_range,  # y
        15.0,             # z (max altitude 15 km)
        300,              # vx (m/s)
        300,              # vy (m/s)
        100               # vz (m/s)
    ]

    # Run LM optimization
    result = least_squares(
        residual_function,
        initial_state,
        args=(track, tx_enu, rx_enu, frequency, antenna_boresight, rx_alt_m),
        bounds=(bounds_lower, bounds_upper),
        method='trf',  # Trust Region Reflective
        ftol=1e-8,
        xtol=1e-8,
        max_nfev=1000
    )

    # Extract results
    state_solution = result.x
    residuals = result.fun
    success = result.success

    # Calculate RMS errors (separate delay, Doppler, antenna, and altitude)
    if antenna_boresight is not None:
        # 4 residuals per detection: delay, doppler, antenna, altitude
        delay_residuals = residuals[0::4]  # Every 4th starting from 0
        doppler_residuals = residuals[1::4]  # Every 4th starting from 1
        antenna_residuals = residuals[2::4]  # Every 4th starting from 2
        altitude_residuals = residuals[3::4]  # Every 4th starting from 3
    else:
        # 3 residuals per detection: delay, doppler, altitude
        delay_residuals = residuals[0::3]  # Every 3rd starting from 0
        doppler_residuals = residuals[1::3]  # Every 3rd starting from 1
        altitude_residuals = residuals[2::3]  # Every 3rd starting from 2

    rms_delay = np.sqrt(np.mean(delay_residuals**2))
    rms_doppler = np.sqrt(np.mean(doppler_residuals**2))

    return {
        'success': success,
        'state': state_solution,
        'residuals': residuals,
        'rms_delay': rms_delay,
        'rms_doppler': rms_doppler,
        'cost': result.cost,
        'message': result.message,
        'nfev': result.nfev
    }


if __name__ == "__main__":
    # Test the solver
    import sys
    sys.path.append('.')
    from config_loader import load_config, load_tracks
    from baseline_geometry import calculate_baseline_geometry
    from initial_guess_single import generate_initial_guess
    from Geometry import Geometry

    print("Testing LM track solver\n")

    # Load config
    config = load_config("config.yml")
    print(config)
    print()

    # Calculate baseline geometry
    geometry = calculate_baseline_geometry(config.rx_lla, config.tx_lla)

    # Convert TX to ENU (in km)
    tx_ecef = Geometry.lla2ecef(config.tx_lla[0], config.tx_lla[1], config.tx_lla[2])
    tx_enu_m = Geometry.ecef2enu(tx_ecef[0], tx_ecef[1], tx_ecef[2],
                                   config.rx_lla[0], config.rx_lla[1], config.rx_lla[2])
    tx_enu = tuple(x / 1000 for x in tx_enu_m)
    rx_enu = (0, 0, 0)

    # Load a track with good SNR
    tracks = load_tracks("events_full_window.jsonl", min_detections=20)

    # Try first few tracks
    for i in range(min(3, len(tracks))):
        track = tracks[i]
        print(f"\n{'='*60}")
        print(f"Track {i+1}: {track}")
        print(f"First detection: {track.detections[0]}")
        print(f"Last detection:  {track.detections[-1]}")

        # Generate initial guess
        initial_guess = generate_initial_guess(
            track, tx_enu, geometry['antenna_boresight_vector'], config.frequency
        )
        print(f"\nInitial guess:")
        print(f"  Position: ({initial_guess[0]:.2f}, {initial_guess[1]:.2f}, {initial_guess[2]:.2f}) km")
        print(f"  Velocity: ({initial_guess[3]:.1f}, {initial_guess[4]:.1f}, {initial_guess[5]:.1f}) m/s")

        # Solve
        print(f"\nSolving...")
        solution = solve_track(track, initial_guess, tx_enu, rx_enu, config.frequency)

        print(f"\nResults:")
        print(f"  Success: {solution['success']}")
        print(f"  Message: {solution['message']}")
        print(f"  Function evaluations: {solution['nfev']}")
        print(f"  Position: ({solution['state'][0]:.2f}, {solution['state'][1]:.2f}, {solution['state'][2]:.2f}) km")
        print(f"  Velocity: ({solution['state'][3]:.1f}, {solution['state'][4]:.1f}, {solution['state'][5]:.1f}) m/s")
        print(f"  RMS delay error: {solution['rms_delay']:.3f} μs")
        print(f"  RMS Doppler error: {solution['rms_doppler']:.3f} Hz")
        print(f"  Cost: {solution['cost']:.6f}")
