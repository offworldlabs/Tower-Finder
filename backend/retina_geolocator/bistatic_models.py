"""
Bistatic radar physics models for delay and Doppler prediction.
"""

import numpy as np
from .Geometry import Geometry


def bistatic_delay(target_enu, tx_enu, rx_enu=(0, 0, 0)):
    """
    Calculate predicted bistatic DIFFERENTIAL delay for a target position.

    This is the delay measured in passive radar:
    delay = (TX→Target→RX path - TX→RX reference path) / c

    Args:
        target_enu: (east, north, up) position of target in km
        tx_enu: (east, north, up) position of transmitter in km
        rx_enu: (east, north, up) position of receiver in km (default origin)

    Returns:
        differential delay in microseconds
    """
    # Convert to numpy arrays for vector operations
    target = np.array(target_enu)
    tx = np.array(tx_enu)
    rx = np.array(rx_enu)

    # Calculate distances
    dist_tx_to_target = np.linalg.norm(target - tx)  # km
    dist_target_to_rx = np.linalg.norm(rx - target)  # km
    dist_tx_to_rx = np.linalg.norm(rx - tx)  # km (baseline/reference path)

    # Bistatic range (surveillance path)
    bistatic_range = dist_tx_to_target + dist_target_to_rx  # km

    # Differential range (compared to reference path)
    differential_range = bistatic_range - dist_tx_to_rx  # km

    # Convert to delay (speed of light = 300 km/ms = 0.3 km/μs)
    c_km_per_us = 0.3  # km/μs
    delay = differential_range / c_km_per_us  # μs

    return delay


def bistatic_doppler(target_enu, velocity_enu, tx_enu, rx_enu, frequency):
    """
    Calculate predicted bistatic Doppler shift for a target.

    Bistatic Doppler has double shift:
    1. TX → Target: Doppler proportional to velocity component toward TX
    2. Target → RX: Doppler proportional to velocity component toward RX

    Args:
        target_enu: (east, north, up) position of target in km
        velocity_enu: (ve, vn, vu) velocity of target in m/s
        tx_enu: (east, north, up) position of transmitter in km
        rx_enu: (east, north, up) position of receiver in km (default origin)
        frequency: transmitter frequency in Hz

    Returns:
        Doppler shift in Hz
    """
    # Convert to numpy arrays
    target = np.array(target_enu)  # km
    velocity = np.array(velocity_enu) / 1000  # convert m/s to km/s
    tx = np.array(tx_enu)  # km
    rx = np.array(rx_enu)  # km

    # Unit vectors from target to TX and to RX
    vec_to_tx = tx - target
    vec_to_rx = rx - target

    dist_to_tx = np.linalg.norm(vec_to_tx)
    dist_to_rx = np.linalg.norm(vec_to_rx)

    if dist_to_tx == 0 or dist_to_rx == 0:
        return 0.0  # Avoid division by zero

    unit_to_tx = vec_to_tx / dist_to_tx
    unit_to_rx = vec_to_rx / dist_to_rx

    # Radial velocity components
    # Positive when target moving toward TX/RX
    v_radial_tx = np.dot(velocity, unit_to_tx)  # km/s
    v_radial_rx = np.dot(velocity, unit_to_rx)  # km/s

    # Doppler shift (double contribution)
    # f_doppler = (f_tx / c) * (v_toward_tx + v_toward_rx)
    c_km_per_s = 299792.458  # km/s
    doppler = (frequency / c_km_per_s) * (v_radial_tx + v_radial_rx)  # Hz

    return doppler


def predict_detection(target_enu, velocity_enu, tx_enu, rx_enu, frequency):
    """
    Predict both delay and Doppler for a detection.

    Args:
        target_enu: (east, north, up) position in km
        velocity_enu: (ve, vn, vu) velocity in m/s
        tx_enu: transmitter position in km
        rx_enu: receiver position in km
        frequency: transmitter frequency in Hz

    Returns:
        (delay_us, doppler_hz) tuple
    """
    delay = bistatic_delay(target_enu, tx_enu, rx_enu)
    doppler = bistatic_doppler(target_enu, velocity_enu, tx_enu, rx_enu, frequency)
    return delay, doppler


if __name__ == "__main__":
    # Test with example scenario
    print("Testing bistatic radar models\n")

    # Setup geometry (using config values converted to ENU relative to RX at origin)
    rx_lla = (33.93918, -84.65191, 290)
    tx_lla = (33.75667, -84.33184, 488)

    # Convert TX to ENU relative to RX
    tx_ecef = Geometry.lla2ecef(tx_lla[0], tx_lla[1], tx_lla[2])
    tx_enu_m = Geometry.ecef2enu(tx_ecef[0], tx_ecef[1], tx_ecef[2],
                                   rx_lla[0], rx_lla[1], rx_lla[2])
    tx_enu = tuple(x / 1000 for x in tx_enu_m)  # Convert m to km

    print(f"RX at origin: (0, 0, 0) km")
    print(f"TX at: ({tx_enu[0]:.2f}, {tx_enu[1]:.2f}, {tx_enu[2]:.2f}) km")
    print()

    # Test target: somewhere between RX and TX, 5 km altitude
    target_enu = (10.0, -10.0, 5.0)  # km
    velocity_enu = (100.0, 50.0, 0.0)  # m/s (eastward and northward)

    print(f"Target at: {target_enu} km")
    print(f"Velocity: {velocity_enu} m/s")
    print()

    # Calculate delay and Doppler
    frequency = 195e6  # Hz

    delay = bistatic_delay(target_enu, tx_enu)
    doppler = bistatic_doppler(target_enu, velocity_enu, tx_enu, (0, 0, 0), frequency)

    print(f"Predicted delay: {delay:.2f} μs")
    print(f"Predicted Doppler: {doppler:.2f} Hz")
    print()

    # Test with different velocities
    print("Doppler vs velocity direction:")
    speed = 100  # m/s
    for angle_deg in [0, 45, 90, 135, 180, 225, 270, 315]:
        angle_rad = np.radians(angle_deg)
        vel = (speed * np.sin(angle_rad), speed * np.cos(angle_rad), 0)
        dop = bistatic_doppler(target_enu, vel, tx_enu, (0, 0, 0), frequency)
        print(f"  Heading {angle_deg:3d}° (speed {speed} m/s): Doppler = {dop:+.2f} Hz")
