"""
Levenberg-Marquardt solver for track-based geolocation.
Fits constant velocity model to time series of bistatic detections.
"""

import numpy as np
from scipy.optimize import least_squares
from .bistatic_models import bistatic_delay, bistatic_doppler
from .baseline_geometry import calculate_target_azimuth, antenna_gain_pattern


# ---------------------------------------------------------------------------
# Vectorised residual – processes all detections in a single numpy pass
# ---------------------------------------------------------------------------

def _residual_vec(state, dt, obs_delay, obs_doppler, tx, rx, dist_tx_rx,
                  frequency, f_over_c, antenna_boresight, antenna_sigma,
                  rx_alt_m, residuals_out):
    """Fully-vectorised residual function.

    Pre-extracted arrays are passed once from *solve_track* so that no Python
    loop or per-detection np.array() allocation happens on the ~20 function
    evaluations that ``least_squares`` performs per solve.
    """
    x0, y0, z0, vx, vy, vz = state

    vx_km = vx / 1000.0
    vy_km = vy / 1000.0
    vz_km = vz / 1000.0

    # Predicted positions  (N, 3) – constant-velocity model
    px = x0 + vx_km * dt
    py = y0 + vy_km * dt
    pz = z0 + vz_km * dt

    # Bistatic delay  (vectorised)
    dx_tx = px - tx[0]; dy_tx = py - tx[1]; dz_tx = pz - tx[2]
    dx_rx = rx[0] - px; dy_rx = rx[1] - py; dz_rx = rx[2] - pz
    dist_tx_t = np.sqrt(dx_tx*dx_tx + dy_tx*dy_tx + dz_tx*dz_tx)
    dist_t_rx = np.sqrt(dx_rx*dx_rx + dy_rx*dy_rx + dz_rx*dz_rx)
    delay_pred = (dist_tx_t + dist_t_rx - dist_tx_rx) / 0.3  # µs

    # Bistatic Doppler  (vectorised)
    inv_tx = 1.0 / dist_tx_t
    inv_rx = 1.0 / dist_t_rx
    # unit vector components  target→TX  and  target→RX
    u_tx_x = dx_tx * inv_tx; u_tx_y = dy_tx * inv_tx; u_tx_z = dz_tx * inv_tx
    u_rx_x = dx_rx * inv_rx; u_rx_y = dy_rx * inv_rx; u_rx_z = dz_rx * inv_rx
    v_radial = (u_tx_x + u_rx_x) * vx_km + (u_tx_y + u_rx_y) * vy_km + (u_tx_z + u_rx_z) * vz_km
    doppler_pred = f_over_c * v_radial

    # Write residuals (delay, doppler, [antenna], altitude) interleaved
    if antenna_boresight is not None:
        stride = 4
        residuals_out[0::stride] = obs_delay - delay_pred
        residuals_out[1::stride] = obs_doppler - doppler_pred

        # Antenna gain  (Gaussian beam pattern)
        az = np.degrees(np.arctan2(px, py))  # target azimuth from RX
        az_diff = np.abs(az - antenna_boresight)
        az_diff = np.where(az_diff > 180, 360 - az_diff, az_diff)
        gain = np.exp(-(az_diff * az_diff) / (2.0 * antenna_sigma * antenna_sigma))
        residuals_out[2::stride] = (1.0 - gain) * 50.0

        # Altitude constraint
        alt_asl = rx_alt_m + pz * 1000.0
        residuals_out[3::stride] = np.where(alt_asl < 50.0, (50.0 - alt_asl) * 0.1, 0.0)
    else:
        stride = 3
        residuals_out[0::stride] = obs_delay - delay_pred
        residuals_out[1::stride] = obs_doppler - doppler_pred

        alt_asl = rx_alt_m + pz * 1000.0
        residuals_out[2::stride] = np.where(alt_asl < 50.0, (50.0 - alt_asl) * 0.1, 0.0)

    # Return a copy — least_squares holds references to previous residual
    # vectors for computing gain ratios; mutating the buffer in-place would
    # corrupt those references.
    return residuals_out.copy()


# Legacy scalar residual – kept for file-based batch processing (process_file)
def residual_function(state, track, tx_enu, rx_enu, frequency, antenna_boresight=None, rx_alt_m=0):
    x0, y0, z0, vx, vy, vz = state
    t0 = track.detections[0].timestamp / 1000
    residuals = []
    for det in track.detections:
        t = det.timestamp / 1000
        dt = t - t0
        pos = np.array([x0 + (vx / 1000) * dt, y0 + (vy / 1000) * dt, z0 + (vz / 1000) * dt])
        vel = np.array([vx, vy, vz])
        delay_pred = bistatic_delay(pos, tx_enu, rx_enu)
        doppler_pred = bistatic_doppler(pos, vel, tx_enu, rx_enu, frequency)
        residuals.append(det.delay - delay_pred)
        residuals.append(det.doppler - doppler_pred)
        if antenna_boresight is not None:
            target_azimuth = calculate_target_azimuth(pos)
            gain = antenna_gain_pattern(target_azimuth, antenna_boresight, beamwidth_deg=48)
            residuals.append((1.0 - gain) * 50.0)
        altitude_asl_m = rx_alt_m + pos[2] * 1000
        if altitude_asl_m < 50:
            residuals.append((50 - altitude_asl_m) * 0.1)
        else:
            residuals.append(0.0)
    return np.array(residuals)


def solve_track(track, initial_state, tx_enu, rx_enu, frequency, antenna_boresight=None, rx_alt_m=0, max_nfev=20):
    """
    Solve for track position and velocity using LM optimization.

    Uses a fully-vectorised residual to avoid per-detection Python loops and
    temporary np.array allocations inside the inner loop of least_squares.
    """
    pos_range = 50  # km
    x0, y0, z0, vx0, vy0, vz0 = initial_state

    min_alt_asl_m = 50
    min_z_enu_km = -(rx_alt_m - min_alt_asl_m) / 1000

    bounds_lower = [x0 - pos_range, y0 - pos_range, min_z_enu_km, -300, -300, -100]
    bounds_upper = [x0 + pos_range, y0 + pos_range, 15.0, 300, 300, 100]

    # --- Pre-extract detection arrays ONCE (never re-allocated per nfev) ---
    dets = track.detections
    N = len(dets)
    t0_s = dets[0].timestamp / 1000.0
    dt = np.empty(N)
    obs_delay = np.empty(N)
    obs_doppler = np.empty(N)
    for i, d in enumerate(dets):
        dt[i] = d.timestamp / 1000.0 - t0_s
        obs_delay[i] = d.delay
        obs_doppler[i] = d.doppler

    tx = np.asarray(tx_enu, dtype=np.float64)
    rx = np.asarray(rx_enu, dtype=np.float64)
    dist_tx_rx = np.linalg.norm(rx - tx)
    f_over_c = frequency / 299792.458  # Hz / (km/s) → 1/km

    antenna_sigma = (48.0 / 2.355) if antenna_boresight is not None else 0.0

    stride = 4 if antenna_boresight is not None else 3
    residuals_buf = np.empty(N * stride)

    result = least_squares(
        _residual_vec,
        initial_state,
        args=(dt, obs_delay, obs_doppler, tx, rx, dist_tx_rx,
              frequency, f_over_c, antenna_boresight, antenna_sigma,
              rx_alt_m, residuals_buf),
        bounds=(bounds_lower, bounds_upper),
        method='trf',
        ftol=1e-4,
        xtol=1e-4,
        max_nfev=max_nfev,
    )

    state_solution = result.x
    residuals = result.fun
    success = result.success

    if antenna_boresight is not None:
        delay_residuals = residuals[0::4]
        doppler_residuals = residuals[1::4]
    else:
        delay_residuals = residuals[0::3]
        doppler_residuals = residuals[1::3]

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
