"""
Coordinate conversion utilities for radar tracking.
Provides LLA (Latitude/Longitude/Altitude) to/from ENU (East/North/Up) conversions
using WGS84 ellipsoid parameters.

Based on proven implementations from blah2-arm and adsb2dd.
"""

import numpy as np

# Constants
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = (1.0 - WGS84_F) * WGS84_A
WGS84_E_SQ = 2.0 * WGS84_F - WGS84_F * WGS84_F

SPEED_OF_LIGHT = 299792458.0
KNOTS_TO_MS = 0.514444
FTMIN_TO_MS = 0.00508


def ft2m(feet):
    """
    Convert feet to meters.

    Args:
        feet: Distance in feet

    Returns:
        Distance in meters
    """
    return feet * 0.3048


def knots_to_ms(knots):
    """
    Convert knots to meters per second.

    Args:
        knots: Speed in knots

    Returns:
        Speed in meters per second
    """
    return knots * KNOTS_TO_MS


def lla2ecef(latitude, longitude, altitude):
    """
    Convert LLA (Latitude/Longitude/Altitude) to ECEF (Earth-Centered Earth-Fixed).
    Uses WGS84 ellipsoid parameters.

    Args:
        latitude: Latitude in degrees
        longitude: Longitude in degrees
        altitude: Altitude in meters above WGS84 ellipsoid

    Returns:
        Tuple of (x, y, z) in meters (ECEF coordinates)
    """
    lat_rad = np.radians(latitude)
    lon_rad = np.radians(longitude)

    # Radius of curvature in prime vertical
    N = WGS84_A / np.sqrt(1.0 - WGS84_E_SQ * np.sin(lat_rad) ** 2)

    # ECEF coordinates
    x = (N + altitude) * np.cos(lat_rad) * np.cos(lon_rad)
    y = (N + altitude) * np.cos(lat_rad) * np.sin(lon_rad)
    z = (N * (1.0 - WGS84_E_SQ) + altitude) * np.sin(lat_rad)

    return x, y, z


def ecef2enu(x, y, z, ref_lat, ref_lon, ref_alt):
    """
    Convert ECEF coordinates to ENU (East/North/Up) relative to a reference point.

    Args:
        x, y, z: ECEF coordinates in meters
        ref_lat: Reference latitude in degrees
        ref_lon: Reference longitude in degrees
        ref_alt: Reference altitude in meters

    Returns:
        Tuple of (east, north, up) in meters
    """
    # Convert reference point to ECEF
    ref_x, ref_y, ref_z = lla2ecef(ref_lat, ref_lon, ref_alt)

    # Vector from reference to point
    dx = x - ref_x
    dy = y - ref_y
    dz = z - ref_z

    # Rotation matrix from ECEF to ENU
    lat_rad = np.radians(ref_lat)
    lon_rad = np.radians(ref_lon)

    sin_lat = np.sin(lat_rad)
    cos_lat = np.cos(lat_rad)
    sin_lon = np.sin(lon_rad)
    cos_lon = np.cos(lon_rad)

    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    return east, north, up


def lla2enu(latitude, longitude, altitude, ref_lat, ref_lon, ref_alt):
    """
    Convert LLA to ENU relative to a reference point.
    Convenience function combining lla2ecef and ecef2enu.

    Args:
        latitude: Latitude in degrees
        longitude: Longitude in degrees
        altitude: Altitude in meters
        ref_lat: Reference latitude in degrees
        ref_lon: Reference longitude in degrees
        ref_alt: Reference altitude in meters

    Returns:
        Tuple of (east, north, up) in meters
    """
    x, y, z = lla2ecef(latitude, longitude, altitude)
    return ecef2enu(x, y, z, ref_lat, ref_lon, ref_alt)


def enu2ecef(east, north, up, ref_lat, ref_lon, ref_alt):
    """
    Convert ENU coordinates to ECEF relative to a reference point.

    Args:
        east, north, up: ENU coordinates in meters
        ref_lat: Reference latitude in degrees
        ref_lon: Reference longitude in degrees
        ref_alt: Reference altitude in meters

    Returns:
        Tuple of (x, y, z) in meters (ECEF coordinates)
    """
    # Convert reference point to ECEF
    ref_x, ref_y, ref_z = lla2ecef(ref_lat, ref_lon, ref_alt)

    # Rotation matrix from ENU to ECEF
    lat_rad = np.radians(ref_lat)
    lon_rad = np.radians(ref_lon)

    sin_lat = np.sin(lat_rad)
    cos_lat = np.cos(lat_rad)
    sin_lon = np.sin(lon_rad)
    cos_lon = np.cos(lon_rad)

    dx = -sin_lon * east - sin_lat * cos_lon * north + cos_lat * cos_lon * up
    dy = cos_lon * east - sin_lat * sin_lon * north + cos_lat * sin_lon * up
    dz = cos_lat * north + sin_lat * up

    x = ref_x + dx
    y = ref_y + dy
    z = ref_z + dz

    return x, y, z


def ecef2lla(x, y, z, max_iter=10, tol=1e-6):
    """
    Convert ECEF to LLA using iterative algorithm.

    Args:
        x, y, z: ECEF coordinates in meters
        max_iter: Maximum iterations for convergence
        tol: Tolerance for convergence in meters

    Returns:
        Tuple of (latitude, longitude, altitude) in degrees and meters
    """
    # Longitude is straightforward
    longitude = np.degrees(np.arctan2(y, x))

    # Iterative solution for latitude and altitude
    p = np.sqrt(x**2 + y**2)

    # Handle pole cases (p ≈ 0) directly to avoid division by zero
    if p < 1e-6:  # Within 1mm of pole
        latitude = 90.0 if z > 0 else -90.0
        altitude = abs(z) - WGS84_B  # Distance from pole
        return latitude, longitude, altitude

    lat = np.arctan2(z, p * (1.0 - WGS84_E_SQ))

    for _ in range(max_iter):
        N = WGS84_A / np.sqrt(1.0 - WGS84_E_SQ * np.sin(lat) ** 2)

        # Protect against division by zero at poles
        cos_lat = np.cos(lat)
        sin_lat = np.sin(lat)
        if abs(cos_lat) < 1e-10:  # Very close to pole
            # Use direct formula for near-pole case
            if abs(sin_lat) < 1e-10:
                # Degenerate case: both sin and cos near zero (should not occur mathematically)
                # This indicates coordinates at or very near Earth's center (x=y=z≈0)
                # Return altitude of 0 at arbitrary lat/lon since position is undefined
                import sys

                print(
                    f"Warning: Degenerate ECEF coordinates ({x:.2f}, {y:.2f}, {z:.2f}) "
                    f"near Earth center. Returning zero altitude.",
                    file=sys.stderr,
                )
                alt = 0.0
            else:
                alt = abs(z) / abs(sin_lat) - N * (1.0 - WGS84_E_SQ)
        else:
            alt = p / cos_lat - N

        lat_new = np.arctan2(z, p * (1.0 - WGS84_E_SQ * N / (N + alt)))

        if abs(lat_new - lat) < tol / WGS84_A:
            lat = lat_new
            break
        lat = lat_new

    latitude = np.degrees(lat)

    # Final altitude calculation
    N = WGS84_A / np.sqrt(1.0 - WGS84_E_SQ * np.sin(lat) ** 2)
    cos_lat = np.cos(lat)
    sin_lat = np.sin(lat)
    if abs(cos_lat) < 1e-10:  # Near pole
        if abs(sin_lat) < 1e-10:
            # Degenerate case already warned about in iteration loop
            altitude = 0.0
        else:
            altitude = abs(z) / abs(sin_lat) - N * (1.0 - WGS84_E_SQ)
    else:
        altitude = p / cos_lat - N

    return latitude, longitude, altitude


def enu2lla(east, north, up, ref_lat, ref_lon, ref_alt):
    """
    Convert ENU to LLA relative to a reference point.
    Convenience function combining enu2ecef and ecef2lla.

    Args:
        east, north, up: ENU coordinates in meters
        ref_lat: Reference latitude in degrees
        ref_lon: Reference longitude in degrees
        ref_alt: Reference altitude in meters

    Returns:
        Tuple of (latitude, longitude, altitude) in degrees and meters
    """
    x, y, z = enu2ecef(east, north, up, ref_lat, ref_lon, ref_alt)
    return ecef2lla(x, y, z)


def enu_velocity_from_adsb(ground_speed_knots, track_degrees, vertical_rate_fpm=0):
    """
    Convert ADS-B velocity data to ENU velocity components.

    Args:
        ground_speed_knots: Ground speed in knots
        track_degrees: Track angle in degrees (0 = North, 90 = East)
        vertical_rate_fpm: Vertical rate in feet per minute (optional)

    Returns:
        Tuple of (vel_east, vel_north, vel_up) in meters per second
    """
    gs_ms = knots_to_ms(ground_speed_knots)
    track_rad = np.radians(track_degrees)

    vel_east = gs_ms * np.sin(track_rad)
    vel_north = gs_ms * np.cos(track_rad)
    vel_up = vertical_rate_fpm * FTMIN_TO_MS if vertical_rate_fpm else 0.0

    return vel_east, vel_north, vel_up


def norm(x, y, z):
    """
    Calculate Euclidean norm of 3D vector.

    Args:
        x, y, z: Vector components

    Returns:
        Norm (magnitude) of vector
    """
    return np.sqrt(x**2 + y**2 + z**2)
