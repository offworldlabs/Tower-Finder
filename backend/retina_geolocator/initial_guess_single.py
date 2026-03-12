"""
Initial guess generator for single-node passive radar geolocation.
Uses bistatic delay ellipsoid and antenna boresight constraint.
"""

import numpy as np
import math
from scipy.optimize import minimize_scalar, minimize
from .bistatic_models import bistatic_delay, bistatic_doppler
from .Geometry import Geometry


def lla_to_enu_km(lat, lon, alt, ref_lat, ref_lon, ref_alt):
    """
    Convert LLA (Latitude/Longitude/Altitude) to ENU (East/North/Up) in kilometers.

    Args:
        lat: Target latitude in degrees
        lon: Target longitude in degrees
        alt: Target altitude in meters
        ref_lat: Reference latitude in degrees
        ref_lon: Reference longitude in degrees
        ref_alt: Reference altitude in meters

    Returns:
        Tuple of (east, north, up) in kilometers
    """
    # Convert target to ECEF
    x, y, z = Geometry.lla2ecef(lat, lon, alt)

    # Convert ECEF to ENU relative to reference point
    east_m, north_m, up_m = Geometry.ecef2enu(x, y, z, ref_lat, ref_lon, ref_alt)

    # Convert meters to kilometers
    return east_m / 1000.0, north_m / 1000.0, up_m / 1000.0


def adsb_velocity_to_enu(gs_knots, track_deg, geom_rate_fpm=None):
    """
    Convert ADS-B velocity data to ENU velocity components.

    Args:
        gs_knots: Ground speed in knots
        track_deg: Track angle in degrees (0 = North, 90 = East)
        geom_rate_fpm: Geometric vertical rate in feet per minute (optional)

    Returns:
        Tuple of (vel_east, vel_north, vel_up) in meters per second,
        or None if inputs produce NaN/Inf
    """
    # Convert knots to m/s
    gs_ms = gs_knots * 0.514444

    # Convert track angle to radians
    track_rad = math.radians(track_deg)

    # Calculate horizontal velocity components
    # Track angle: 0° = North, 90° = East (aviation convention)
    vel_east = gs_ms * math.sin(track_rad)
    vel_north = gs_ms * math.cos(track_rad)

    # Vertical velocity (optional)
    if geom_rate_fpm is not None:
        # Convert feet/min to m/s
        vel_up = geom_rate_fpm * 0.00508
    else:
        vel_up = 0.0

    # Check for NaN/Inf
    if any(math.isnan(v) or math.isinf(v) for v in [vel_east, vel_north, vel_up]):
        return None

    return vel_east, vel_north, vel_up


def ellipsoid_boresight_intersection(bistatic_range_km, tx_enu, boresight_vector, altitude_km):
    """
    Find intersection of bistatic ellipsoid with ray along antenna boresight at given altitude.

    The ellipsoid is defined by: |TX-p| + |p-RX| = bistatic_range
    The ray is: p = t * boresight_vector, where t > 0
    We search for the value of t that satisfies the ellipsoid equation at the given altitude.

    Args:
        bistatic_range_km: Total path length TX→Target→RX in km
        tx_enu: (east, north, up) position of TX in km
        boresight_vector: Unit vector in boresight direction
        altitude_km: Altitude to solve for in km

    Returns:
        (east, north, up) position in km, or None if no solution
    """
    rx_enu = np.array([0, 0, 0])  # RX at origin
    tx = np.array(tx_enu)
    boresight = np.array(boresight_vector)

    # Normalize boresight to horizontal (project onto E-N plane)
    boresight_horiz = np.array([boresight[0], boresight[1], 0])
    boresight_horiz = boresight_horiz / np.linalg.norm(boresight_horiz)

    # Define cost function: how far from desired bistatic range
    def cost(t):
        # Position along boresight at given altitude
        # t is the horizontal distance along boresight
        pos = np.array([
            t * boresight_horiz[0],
            t * boresight_horiz[1],
            altitude_km
        ])

        # Calculate bistatic range for this position
        dist_tx_to_target = np.linalg.norm(pos - tx)
        dist_target_to_rx = np.linalg.norm(pos - rx_enu)
        predicted_range = dist_tx_to_target + dist_target_to_rx

        # Return squared error
        return (predicted_range - bistatic_range_km)**2

    # Search for solution along boresight (0 to 100 km range)
    result = minimize_scalar(cost, bounds=(0, 100), method='bounded')

    if result.fun < 0.01:  # Within 100m error
        t = result.x
        pos = np.array([
            t * boresight_horiz[0],
            t * boresight_horiz[1],
            altitude_km
        ])
        return tuple(pos)
    return None


def generate_initial_guess(track, tx_enu, boresight_vector, frequency):
    """
    Generate initial guess for track using first detection.

    Strategy:
    1. Use first detection's delay to define bistatic ellipsoid
    2. Assume target on antenna boresight
    3. Try multiple altitudes, pick most plausible
    4. Use zero velocity as starting point

    Args:
        track: Track object with detections
        tx_enu: (east, north, up) position of TX in km
        boresight_vector: Unit vector in boresight direction
        frequency: TX frequency in Hz

    Returns:
        initial_state: [x0, y0, z0, vx, vy, vz] in km and m/s
    """
    # Use first detection
    first_det = track.detections[0]

    # Convert delay to differential bistatic range
    rx_enu = np.array([0, 0, 0])
    tx = np.array(tx_enu)
    baseline_dist = np.linalg.norm(tx - rx_enu)  # km
    differential_range = first_det.delay * 0.3  # μs * 0.3 km/μs = km
    bistatic_range = baseline_dist + differential_range  # km (total path)

    # Use fixed altitude (altitude not very accurate in this model)
    altitude = 2.0  # km

    pos_guess = ellipsoid_boresight_intersection(bistatic_range, tx_enu, boresight_vector, altitude)

    if pos_guess is None:
        # Fallback: estimate position along boresight
        horiz_dist = bistatic_range / 2
        boresight_horiz = np.array([boresight_vector[0], boresight_vector[1], 0])
        boresight_horiz = boresight_horiz / np.linalg.norm(boresight_horiz)
        pos_guess = (
            horiz_dist * boresight_horiz[0],
            horiz_dist * boresight_horiz[1],
            altitude
        )

    # Velocity: start with zero (let solver find it)
    # Alternative: could estimate from Doppler, but heading is unknown
    vel_guess = (0, 0, 0)  # m/s

    # Combine into state vector
    initial_state = [
        pos_guess[0], pos_guess[1], pos_guess[2],  # position in km
        vel_guess[0], vel_guess[1], vel_guess[2]   # velocity in m/s
    ]

    return initial_state


def generate_adsb_initial_guess(track, rx_lla, config):
    """
    Generate initial guess from ADS-B data in first detection with ADS-B.

    Uses ADS-B position and velocity data to create a high-quality initial guess
    for the solver. This typically results in faster convergence (3-5 iterations)
    compared to geometric guess (10-20 iterations).

    Args:
        track: Track object with detections (must have ADS-B data)
        rx_lla: Tuple of (latitude, longitude, altitude) for receiver in degrees and meters
        config: Configuration object (not currently used, reserved for future)

    Returns:
        initial_state: [x0, y0, z0, vx, vy, vz] in km and m/s, or None if invalid
    """
    # Find first detection with ADS-B data
    first_det_with_adsb = None
    for det in track.detections:
        if det.adsb is not None:
            first_det_with_adsb = det
            break

    if first_det_with_adsb is None:
        return None

    adsb = first_det_with_adsb.adsb

    # Validate required fields
    if 'lat' not in adsb or 'lon' not in adsb:
        return None

    # Get position
    lat = adsb['lat']
    lon = adsb['lon']
    alt_feet = adsb.get('alt_baro', 0)  # ADS-B altitude in feet
    alt_meters = alt_feet * 0.3048  # Convert feet to meters

    # Convert position: LLA → ENU (km)
    try:
        x, y, z = lla_to_enu_km(lat, lon, alt_meters, rx_lla[0], rx_lla[1], rx_lla[2])
    except Exception:
        return None

    # Check for NaN/Inf in position
    if any(math.isnan(v) or math.isinf(v) for v in [x, y, z]):
        return None

    # Derive velocity from ground speed + track angle (if available)
    vx, vy, vz = 0.0, 0.0, 0.0  # Default to zero velocity

    if 'gs' in adsb and 'track' in adsb:
        gs = adsb['gs']
        track_angle = adsb['track']
        geom_rate = adsb.get('geom_rate')  # Optional vertical rate

        # Convert velocity
        vel_result = adsb_velocity_to_enu(gs, track_angle, geom_rate)

        if vel_result is not None:
            vx, vy, vz = vel_result

    # Final validation: ensure no NaN/Inf in state vector
    if any(math.isnan(v) or math.isinf(v) for v in [x, y, z, vx, vy, vz]):
        return None

    # Return state vector
    return [x, y, z, vx, vy, vz]


def select_initial_guess(track, tx_enu, boresight_vector, frequency, config, rx_lla):
    """
    Route to appropriate initial guess generator based on data availability.

    Tries ADS-B initial guess first (if enabled and available), then falls back
    to geometric guess if needed. This provides the best initial guess available
    while maintaining backward compatibility.

    Args:
        track: Track object with detections
        tx_enu: (east, north, up) position of TX in km
        boresight_vector: Unit vector in boresight direction
        frequency: TX frequency in Hz
        config: GeolocatorConfig object (may not have ADS-B fields yet)
        rx_lla: Tuple of (latitude, longitude, altitude) for receiver

    Returns:
        Tuple of (initial_state, source) where:
            - initial_state: [x0, y0, z0, vx, vy, vz] in km and m/s
            - source: "adsb" or "geometric"

    Raises:
        ValueError: If ADS-B guess fails and fallback is disabled
    """
    # Get config options with defaults (for backward compatibility before issue #5)
    use_adsb = getattr(config, 'use_adsb_initial_guess', True)
    adsb_fallback = getattr(config, 'adsb_fallback_to_geometric', True)

    # Try ADS-B if enabled and data available
    if use_adsb and track.adsb_initialized:
        guess = generate_adsb_initial_guess(track, rx_lla, config)

        if guess is not None:
            return guess, "adsb"

        # ADS-B guess failed
        if not adsb_fallback:
            raise ValueError(
                f"ADS-B initial guess failed for track {track.track_id}, "
                "fallback to geometric disabled"
            )

    # Geometric guess (current method)
    guess = generate_initial_guess(track, tx_enu, boresight_vector, frequency)
    return guess, "geometric"


def generate_multi_start_guesses(track, tx_enu, boresight_vector, frequency, n_starts=5):
    """
    Generate multiple initial guesses for multi-start optimization.

    Args:
        track: Track object
        tx_enu: TX position in km
        boresight_vector: Antenna boresight direction
        frequency: TX frequency in Hz
        n_starts: Number of initial guesses to generate

    Returns:
        List of initial_state vectors
    """
    first_det = track.detections[0]
    rx_enu = np.array([0, 0, 0])
    tx = np.array(tx_enu)
    baseline_dist = np.linalg.norm(tx - rx_enu)
    differential_range = first_det.delay * 0.3
    bistatic_range = baseline_dist + differential_range  # km

    guesses = []

    # Try different altitudes
    altitudes = np.linspace(1, 10, n_starts)

    for alt in altitudes:
        pos = ellipsoid_boresight_intersection(bistatic_range, tx_enu, boresight_vector, alt)
        if pos is not None:
            # Try different velocity directions (along boresight, perpendicular, etc.)
            vel_guess = (0, 0, 0)  # Start with zero velocity
            initial_state = [
                pos[0], pos[1], pos[2],
                vel_guess[0], vel_guess[1], vel_guess[2]
            ]
            guesses.append(initial_state)

    if len(guesses) == 0:
        # Fallback
        guesses.append(generate_initial_guess(track, tx_enu, boresight_vector, frequency))

    return guesses


if __name__ == "__main__":
    # Test initial guess generation
    import sys
    sys.path.append('.')
    from config_loader import load_config, load_tracks, Detection, Track
    from baseline_geometry import calculate_baseline_geometry
    from Geometry import Geometry

    print("Testing initial guess generation\n")

    # Load config
    config = load_config("config.yml")
    print(config)
    print()

    # Calculate baseline geometry
    geometry = calculate_baseline_geometry(config.rx_lla, config.tx_lla)
    print(f"Antenna boresight: {geometry['antenna_boresight']:.2f}°")
    print(f"Boresight vector: {geometry['antenna_boresight_vector']}")
    print()

    # Convert TX to ENU (in km)
    tx_ecef = Geometry.lla2ecef(config.tx_lla[0], config.tx_lla[1], config.tx_lla[2])
    tx_enu_m = Geometry.ecef2enu(tx_ecef[0], tx_ecef[1], tx_ecef[2],
                                   config.rx_lla[0], config.rx_lla[1], config.rx_lla[2])
    tx_enu = tuple(x / 1000 for x in tx_enu_m)

    # Load a track
    tracks = load_tracks("events_full_window.jsonl", min_detections=20)
    test_track = tracks[0]
    print(f"Test track: {test_track}")
    print(f"First detection: {test_track.detections[0]}")
    print()

    # Generate initial guess
    initial_guess = generate_initial_guess(
        test_track, tx_enu, geometry['antenna_boresight_vector'], config.frequency
    )

    print("Initial guess:")
    print(f"  Position: ({initial_guess[0]:.2f}, {initial_guess[1]:.2f}, {initial_guess[2]:.2f}) km")
    print(f"  Velocity: ({initial_guess[3]:.1f}, {initial_guess[4]:.1f}, {initial_guess[5]:.1f}) m/s")
    print()

    # Verify the guess produces reasonable delay
    predicted_delay = bistatic_delay(
        initial_guess[0:3], tx_enu, (0, 0, 0)
    )
    measured_delay = test_track.detections[0].delay
    print(f"Measured delay: {measured_delay:.2f} μs")
    print(f"Predicted delay from guess: {predicted_delay:.2f} μs")
    print(f"Error: {abs(predicted_delay - measured_delay):.2f} μs")
