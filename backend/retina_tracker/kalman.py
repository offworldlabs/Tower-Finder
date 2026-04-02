"""Kalman filter for constant velocity model in delay-Doppler space."""

import sys

import numpy as np

from .config import (
    MEASUREMENT_NOISE_DELAY,
    MEASUREMENT_NOISE_DOPPLER,
    PROCESS_NOISE_DELAY,
    PROCESS_NOISE_DOPPLER,
)


class KalmanFilter:
    """2D Kalman filter for constant velocity model in delay-Doppler space.

    State vector: [delay, delay_rate, doppler, doppler_rate]
    """

    def __init__(self, dt=0.5):
        self.dt = dt
        self.dim_state = 4
        self.dim_meas = 2

        self.F = np.array([[1, dt, 0, 0], [0, 1, 0, 0], [0, 0, 1, dt], [0, 0, 0, 1]])
        self.H = np.array([[1, 0, 0, 0], [0, 0, 1, 0]])

        q_delay = PROCESS_NOISE_DELAY()
        q_doppler = PROCESS_NOISE_DOPPLER()
        self.Q = np.array(
            [
                [q_delay * dt**3 / 3, q_delay * dt**2 / 2, 0, 0],
                [q_delay * dt**2 / 2, q_delay * dt, 0, 0],
                [0, 0, q_doppler * dt**3 / 3, q_doppler * dt**2 / 2],
                [0, 0, q_doppler * dt**2 / 2, q_doppler * dt],
            ]
        )

        self.R = np.diag([MEASUREMENT_NOISE_DELAY, MEASUREMENT_NOISE_DOPPLER])

    def predict(self, state, covariance):
        dt = self.dt
        # Rebuild F and Q with the current dt so predictions match the actual
        # frame interval (which can vary from 0.5 s for real nodes to 40 s for
        # synthetic fleet nodes).  Uses in-place writes to pre-allocated
        # arrays to avoid per-call np.array() allocation overhead.
        F = self.F
        F[0, 1] = dt
        F[2, 3] = dt
        q_delay = PROCESS_NOISE_DELAY()
        q_doppler = PROCESS_NOISE_DOPPLER()
        dt2 = dt * dt
        dt3 = dt2 * dt
        Q = self.Q
        Q[0, 0] = q_delay * dt3 / 3
        Q[0, 1] = Q[1, 0] = q_delay * dt2 / 2
        Q[1, 1] = q_delay * dt
        Q[2, 2] = q_doppler * dt3 / 3
        Q[2, 3] = Q[3, 2] = q_doppler * dt2 / 2
        Q[3, 3] = q_doppler * dt

        state_pred = F @ state
        cov_pred = F @ covariance @ F.T + Q

        if state_pred[0] < 0:
            state_pred[0] = 0.0
            if state_pred[1] < 0:
                state_pred[1] = 0.0

        return state_pred, cov_pred

    def update(self, state, covariance, measurement, snr=None):
        R = self.R.copy()
        if snr is not None:
            snr_linear = 10 ** (snr / 10)
            noise_scale = 1.0 / max(snr_linear / 10, 0.1)
            R = R * noise_scale

        z_pred = self.H @ state
        innovation = measurement - z_pred
        S = self.H @ covariance @ self.H.T + R

        try:
            K = covariance @ self.H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            print("Warning: Singular innovation covariance in Kalman update, skipping measurement", file=sys.stderr)
            return state, covariance

        state_upd = state + K @ innovation
        cov_upd = (np.eye(self.dim_state) - K @ self.H) @ covariance

        return state_upd, cov_upd

    def get_innovation_covariance(self, covariance):
        S = self.H @ covariance @ self.H.T + self.R
        return S
