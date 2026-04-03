"""Core Tracker class and GNN data association logic."""

import sys

import numpy as np
from scipy.optimize import linear_sum_assignment

from .config import (
    GATE_THRESHOLD,
    M_THRESHOLD,
    MIN_SNR,
    TRACKLET_MAX_DELAY_RESIDUAL,
    TRACKLET_MAX_DOPPLER_RESIDUAL,
    TRACKLET_MAX_TIME_SPAN,
    get_config,
)
from .kalman import KalmanFilter
from .track import Track, TrackState


class Tracker:
    """Multi-target tracker using Kalman filtering and GNN data association."""

    def __init__(self, event_writer=None, detection_window=20, config=None):
        self.kf = KalmanFilter()
        self.tracks = []
        self.all_tracks = []
        self.last_timestamp = None
        self.detection_window = detection_window
        self.frame_count = 0
        self.event_writer = event_writer
        self.config = config if config else get_config()

    def process_frame(self, detections, timestamp):
        self.frame_count += 1

        if self.last_timestamp is not None:
            dt = (timestamp - self.last_timestamp) / 1000.0
        else:
            dt = 0.5

        detections = [d for d in detections if d["snr"] >= MIN_SNR()]

        for track in self.tracks:
            track.predict(dt)

        associations = self._associate(detections)

        associated_tracks = set()
        associated_detections = set()

        _lazy_write = hasattr(self.event_writer, 'write_event_lazy') if self.event_writer else False

        for track_idx, det_idx in associations:
            track = self.tracks[track_idx]
            det = detections[det_idx]
            track.update(det, timestamp, frame=self.frame_count)
            if track.state_status == TrackState.COASTING:
                track.state_status = TrackState.ACTIVE
            associated_tracks.add(track_idx)
            associated_detections.add(det_idx)

            if track.id and self.event_writer:
                _det_n = min(track.n_associated, self.detection_window)
                if _lazy_write:
                    self.event_writer.write_event_lazy(
                        track.id,
                        timestamp,
                        _det_n,
                        track,
                        adsb_hex=track.adsb_hex,
                        adsb_initialized=track.adsb_initialized,
                        is_anomalous=track.is_anomalous,
                        max_velocity_ms=track.max_velocity_ms,
                    )
                else:
                    detections_window = track.get_recent_detections(n=self.detection_window)
                    self.event_writer.write_event(
                        track.id,
                        timestamp,
                        track.n_associated,
                        detections_window,
                        adsb_hex=track.adsb_hex,
                        adsb_initialized=track.adsb_initialized,
                        is_anomalous=track.is_anomalous,
                        max_velocity_ms=track.max_velocity_ms,
                    )

        for i, track in enumerate(self.tracks):
            if i not in associated_tracks:
                track.mark_missed(timestamp, frame=self.frame_count)
                if track.state_status == TrackState.ACTIVE:
                    track.state_status = TrackState.COASTING

        for track in self.tracks:
            promoted = track.promote_if_ready()
            if promoted:
                track.id = Track._generate_id(timestamp, adsb_hex=track.adsb_hex)
                if self.event_writer:
                    _det_n = min(track.n_associated, self.detection_window)
                    if _lazy_write:
                        self.event_writer.write_event_lazy(
                            track.id,
                            timestamp,
                            _det_n,
                            track,
                            adsb_hex=track.adsb_hex,
                            adsb_initialized=track.adsb_initialized,
                            is_anomalous=track.is_anomalous,
                            max_velocity_ms=track.max_velocity_ms,
                        )
                    else:
                        detections_list = track.get_recent_detections(n=_det_n)
                        self.event_writer.write_event(
                            track.id,
                            timestamp,
                            _det_n,
                            detections_list,
                            adsb_hex=track.adsb_hex,
                            adsb_initialized=track.adsb_initialized,
                            is_anomalous=track.is_anomalous,
                            max_velocity_ms=track.max_velocity_ms,
                        )

        for i, det in enumerate(detections):
            if i not in associated_detections:
                new_track = Track(det, timestamp, self.kf, frame=self.frame_count, config=self.config)
                self.tracks.append(new_track)

        self._initiate_tracklets(timestamp)

        deleted_tracks = [t for t in self.tracks if t.should_delete()]
        for track in deleted_tracks:
            if track.state_status == TrackState.ACTIVE or track.n_associated >= M_THRESHOLD():
                self.all_tracks.append(track)
        self.tracks = [t for t in self.tracks if not t.should_delete()]

        # Prune all_tracks to the merge-window (5 s = 5000 ms) so _merge_tracks
        # stays O(window²) instead of O(uptime²).  Entries older than the window
        # can never be paired with new tracks, so they are safe to discard.
        _MERGE_WINDOW_MS = 5000
        if self.all_tracks:
            cutoff = timestamp - _MERGE_WINDOW_MS
            self.all_tracks = [t for t in self.all_tracks if t.death_timestamp >= cutoff]

        if len(self.all_tracks) > 1:
            self._merge_tracks()

        self.last_timestamp = timestamp

    def _associate(self, detections):
        if not self.tracks or not detections:
            return []

        n_tracks = len(self.tracks)
        n_dets = len(detections)
        cost_matrix = np.full((n_tracks, n_dets), 1e6)

        # Pre-compute detection measurements as a single (n_dets, 2) array
        # to avoid creating n_tracks × n_dets individual numpy arrays.
        det_z = np.array([[d["delay"], d["doppler"]] for d in detections])
        det_snr = np.array([d.get("snr", 10.0) for d in detections])
        snr_weights = 20.0 / np.maximum(det_snr, 5.0)

        base_gate = GATE_THRESHOLD()
        adsb_priority = self.config.get("adsb", {}).get("priority")

        for i, track in enumerate(self.tracks):
            z_pred = track.get_predicted_measurement()
            S = track.get_innovation_covariance()

            # Analytical 2×2 inverse (avoids numpy.linalg.inv per-track overhead)
            det_S = S[0, 0] * S[1, 1] - S[0, 1] * S[1, 0]
            if abs(det_S) < 1e-15:
                continue
            inv_det = 1.0 / det_S
            S_inv = np.array([
                [S[1, 1] * inv_det, -S[0, 1] * inv_det],
                [-S[1, 0] * inv_det, S[0, 0] * inv_det],
            ])

            gate = base_gate
            if track.state_status == TrackState.COASTING and track.n_missed > 0:
                gate = base_gate * min(1.0 + 0.1 * track.n_missed, 1.2)

            # Vectorized Mahalanobis distance for all detections at once
            innovations = det_z - z_pred                     # (n_dets, 2)
            tmp = innovations @ S_inv                        # (n_dets, 2)
            mahal = np.sum(tmp * innovations, axis=1)        # (n_dets,)

            within_gate = mahal < gate
            if not np.any(within_gate):
                continue

            costs = mahal * snr_weights
            if adsb_priority and track.adsb_initialized:
                costs *= 0.8
            # Write only gated entries into cost matrix
            mask_indices = np.where(within_gate)[0]
            cost_matrix[i, mask_indices] = costs[mask_indices]

        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        associations = [(r, c) for r, c in zip(row_ind, col_ind) if cost_matrix[r, c] < 1e6]

        return associations

    def _initiate_tracklets(self, timestamp):
        for track in self.tracks:
            if track.state_status != TrackState.TENTATIVE:
                continue
            if track.n_associated < 3:
                continue

            measurements = track.history["measurements"]
            timestamps = track.history["timestamps"]

            assoc_indices = [i for i, m in enumerate(measurements) if m is not None]
            if len(assoc_indices) < 3:
                continue

            last_3_indices = assoc_indices[-3:]

            delays = []
            dopplers = []
            times = []

            for idx in last_3_indices:
                m = measurements[idx]
                t = timestamps[idx]
                delays.append(m["delay"])
                dopplers.append(m["doppler"])
                times.append(t / 1000.0)

            dt_total = times[-1] - times[0]
            if dt_total > TRACKLET_MAX_TIME_SPAN():
                continue
            if dt_total < 0.1:
                continue

            delay_velocity = (delays[-1] - delays[0]) / dt_total
            doppler_velocity = (dopplers[-1] - dopplers[0]) / dt_total

            delay_residuals = []
            doppler_residuals = []

            for i in range(len(times)):
                dt = times[i] - times[0]
                pred_delay = delays[0] + delay_velocity * dt
                pred_doppler = dopplers[0] + doppler_velocity * dt

                delay_residuals.append(abs(delays[i] - pred_delay))
                doppler_residuals.append(abs(dopplers[i] - pred_doppler))

            max_delay_residual = max(delay_residuals)
            max_doppler_residual = max(doppler_residuals)

            if (
                max_delay_residual < TRACKLET_MAX_DELAY_RESIDUAL()
                and max_doppler_residual < TRACKLET_MAX_DOPPLER_RESIDUAL()
            ):
                track.state_status = TrackState.ACTIVE

                track.state[1] = delay_velocity
                track.state[3] = doppler_velocity

                track.id = Track._generate_id(timestamp, adsb_hex=track.adsb_hex)

                if self.event_writer:
                    detections_list = track.get_recent_detections(n=track.n_associated)
                    self.event_writer.write_event(
                        track.id,
                        timestamp,
                        track.n_associated,
                        detections_list,
                        adsb_hex=track.adsb_hex,
                        adsb_initialized=track.adsb_initialized,
                        is_anomalous=track.is_anomalous,
                        max_velocity_ms=track.max_velocity_ms,
                    )

    def _merge_tracks(self):
        if len(self.all_tracks) < 2:
            return

        merged_indices = set()

        for i in range(len(self.all_tracks)):
            if i in merged_indices:
                continue

            track_a = self.all_tracks[i]

            for j in range(i + 1, len(self.all_tracks)):
                if j in merged_indices:
                    continue

                track_b = self.all_tracks[j]

                time_gap = abs(track_a.death_timestamp - track_b.birth_timestamp)
                if time_gap > 5000:
                    continue

                end_state_a = track_a.history["states"][-1]
                start_state_b = track_b.history["states"][0]

                delay_diff = abs(end_state_a[0] - start_state_b[0])
                doppler_diff = abs(end_state_a[2] - start_state_b[2])

                if delay_diff < 5.0 and doppler_diff < 50.0:
                    self._merge_track_pair(track_a, track_b)
                    merged_indices.add(j)
                    break

        self.all_tracks = [t for i, t in enumerate(self.all_tracks) if i not in merged_indices]

    def _merge_track_pair(self, track_a, track_b):
        track_a.history["timestamps"].extend(track_b.history["timestamps"])
        track_a.history["states"].extend(track_b.history["states"])
        track_a.history["measurements"].extend(track_b.history["measurements"])
        track_a.history["state_status"].extend(track_b.history["state_status"])

        track_a.n_frames += track_b.n_frames
        track_a.n_associated += track_b.n_associated
        track_a.total_snr += track_b.total_snr
        track_a.death_timestamp = track_b.death_timestamp

        track_a.state = track_b.state
        track_a.covariance = track_b.covariance

    def get_tracks(self):
        return self.tracks

    def get_active_tracks(self):
        return [t for t in self.tracks if t.state_status == TrackState.ACTIVE]

    def get_all_tracks(self):
        return self.tracks

    def get_confirmed_tracks(self):
        confirmed = []
        confirmed.extend([t for t in self.tracks if t.state_status in (TrackState.ACTIVE, TrackState.COASTING)])
        confirmed.extend(self.all_tracks)
        return confirmed

    def to_dict(self):
        all_confirmed = self.get_confirmed_tracks()
        return {
            "tracks": [t.to_dict() for t in all_confirmed],
            "n_tracks": len(all_confirmed),
            "n_active": len(self.get_active_tracks()),
        }
