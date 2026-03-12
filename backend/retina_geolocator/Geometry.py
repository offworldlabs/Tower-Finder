"""
@file Geometry.py
@brief WGS84 geodetic coordinate transforms (standalone implementation).

Provides LLA <-> ECEF <-> ENU conversions using the WGS84 ellipsoid.
"""

import numpy as np


class Geometry:
    """WGS84 geodetic coordinate transformations."""

    # WGS84 ellipsoid parameters
    _a = 6378137.0            # semi-major axis (m)
    _f = 1 / 298.257223563    # flattening
    _b = _a * (1 - _f)        # semi-minor axis
    _e2 = 2 * _f - _f ** 2    # first eccentricity squared

    @staticmethod
    def lla2ecef(lat_deg, lon_deg, alt_m):
        """Convert LLA (degrees, meters) to ECEF (meters).

        Returns:
            (x, y, z) in meters
        """
        lat = np.radians(lat_deg)
        lon = np.radians(lon_deg)
        sin_lat = np.sin(lat)
        cos_lat = np.cos(lat)
        sin_lon = np.sin(lon)
        cos_lon = np.cos(lon)

        N = Geometry._a / np.sqrt(1 - Geometry._e2 * sin_lat ** 2)

        x = (N + alt_m) * cos_lat * cos_lon
        y = (N + alt_m) * cos_lat * sin_lon
        z = (N * (1 - Geometry._e2) + alt_m) * sin_lat

        return (x, y, z)

    @staticmethod
    def ecef2lla(x, y, z):
        """Convert ECEF (meters) to LLA (degrees, meters).

        Uses iterative (Bowring) method.

        Returns:
            (lat_deg, lon_deg, alt_m)
        """
        a = Geometry._a
        e2 = Geometry._e2
        b = Geometry._b

        lon = np.arctan2(y, x)
        p = np.sqrt(x ** 2 + y ** 2)

        # Initial estimate
        lat = np.arctan2(z, p * (1 - e2))

        for _ in range(10):
            sin_lat = np.sin(lat)
            N = a / np.sqrt(1 - e2 * sin_lat ** 2)
            lat_new = np.arctan2(z + e2 * N * sin_lat, p)
            if abs(lat_new - lat) < 1e-12:
                break
            lat = lat_new

        sin_lat = np.sin(lat)
        cos_lat = np.cos(lat)
        N = a / np.sqrt(1 - e2 * sin_lat ** 2)

        if abs(cos_lat) > 1e-10:
            alt = p / cos_lat - N
        else:
            alt = abs(z) - b

        return (np.degrees(lat), np.degrees(lon), alt)

    @staticmethod
    def ecef2enu(x, y, z, ref_lat_deg, ref_lon_deg, ref_alt_m):
        """Convert ECEF to ENU relative to a reference LLA point.

        Returns:
            (east, north, up) in meters
        """
        ref_x, ref_y, ref_z = Geometry.lla2ecef(ref_lat_deg, ref_lon_deg, ref_alt_m)
        dx = x - ref_x
        dy = y - ref_y
        dz = z - ref_z

        lat = np.radians(ref_lat_deg)
        lon = np.radians(ref_lon_deg)
        sin_lat = np.sin(lat)
        cos_lat = np.cos(lat)
        sin_lon = np.sin(lon)
        cos_lon = np.cos(lon)

        east = -sin_lon * dx + cos_lon * dy
        north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
        up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

        return (east, north, up)

    @staticmethod
    def enu2ecef(east, north, up, ref_lat_deg, ref_lon_deg, ref_alt_m):
        """Convert ENU to ECEF given a reference LLA point.

        Returns:
            (x, y, z) in meters
        """
        ref_x, ref_y, ref_z = Geometry.lla2ecef(ref_lat_deg, ref_lon_deg, ref_alt_m)

        lat = np.radians(ref_lat_deg)
        lon = np.radians(ref_lon_deg)
        sin_lat = np.sin(lat)
        cos_lat = np.cos(lat)
        sin_lon = np.sin(lon)
        cos_lon = np.cos(lon)

        dx = -sin_lon * east - sin_lat * cos_lon * north + cos_lat * cos_lon * up
        dy = cos_lon * east - sin_lat * sin_lon * north + cos_lat * sin_lon * up
        dz = cos_lat * north + sin_lat * up

        return (ref_x + dx, ref_y + dy, ref_z + dz)
