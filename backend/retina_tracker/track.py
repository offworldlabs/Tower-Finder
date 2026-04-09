"""Track class and state management for radar tracking."""

from datetime import datetime
from enum import Enum

import numpy as np

from . import geometry
from .config import (
    KNOTS_TO_MS,
    MACH_1_MS,
    MAX_DIRECTION_CHANGE_DEG_PER_SEC,
    MAX_NORMAL_ACCEL_MS2,
    SPEED_OF_LIGHT,
    M_THRESHOLD,
    N_DELETE,
    N_WINDOW,
    _get_param,
    get_mach1_doppler_threshold,
    ORBIT_HEADING_WINDOW,
    ORBIT_MIN_CUMULATIVE_DEG,
    SPOOF_POSITION_EPSILON_DEG,
    SPOOF_MIN_SPEED_KTS,
    SPOOF_MIN_FROZEN_FRAMES,
    ALTITUDE_JUMP_THRESHOLD_FT,
    ANOMALOUS_ACCEL_MS2,
    LONG_HOVER_POSITION_EPSILON_DEG,
    LONG_HOVER_MIN_DURATION_S,
)


class TrackState(Enum):
    """Track states following blah2 architecture."""

    TENTATIVE = 0
    ASSOCIATED = 1
    ACTIVE = 2
    COASTING = 3


class Track:
    """Represents a single radar track with state history."""

    _daily_counter = 0
    _last_date = None

    def __init__(self, detection, timestamp, kf, frame=0, config=None):
        self.id = None
        self.state_status = TrackState.TENTATIVE
        self.kf = kf
        self.adsb_hex = None
        self.adsb_initialized = False

        adsb_config = config.get("adsb", {}) if config else {}
        if detection.get("adsb") and adsb_config.get("enabled") and adsb_config.get("reference_location"):
            self._init_from_adsb(detection, adsb_config)
        else:
            self._init_from_delay_doppler(detection)

        # Capture ADS-B hex even when not using ADS-B for state initialization
        if self.adsb_hex is None and detection.get("adsb"):
            _det_hex = detection["adsb"].get("hex")
            if _det_hex and isinstance(_det_hex, str):
                self.adsb_hex = _det_hex

        self.history = {
            "timestamps": [timestamp],
            "frames": [frame],
            "states": [self.state.copy()],
            "measurements": [detection],
            "state_status": [self.state_status.name],
        }

        self.n_frames = 1
        self.n_associated = 0
        self.n_missed = 0

        self.total_snr = detection["snr"]
        self.birth_timestamp = timestamp
        self.death_timestamp = timestamp

        self.is_anomalous = False
        self.max_velocity_ms = 0.0
        self.anomaly_detections = []
        self.anomaly_types = set()
        self.last_velocity_ms = None
        self.last_heading_deg = None
        self._heading_changes = []
        self._last_adsb_lat = None
        self._last_adsb_lon = None
        self._adsb_frozen_count = 0
        self._last_alt_baro = None
        self._hover_anchor_lat = None
        self._hover_anchor_lon = None
        self._hover_start_ts = None
        self._check_velocity_anomaly(detection, timestamp)
        self._check_doppler_anomaly(detection)
        # Store initial ADS-B reference values for change-detection checks
        _init_adsb = detection.get("adsb")
        if _init_adsb and isinstance(_init_adsb, dict) and self._validate_adsb_data(_init_adsb):
            self._last_adsb_lat = _init_adsb.get("lat")
            self._last_adsb_lon = _init_adsb.get("lon")
            self._last_alt_baro = _init_adsb.get("alt_baro")
            _hdg = _init_adsb.get("track")
            if _hdg is not None and isinstance(_hdg, (int, float)) and not np.isnan(_hdg):
                self.last_heading_deg = _hdg

    @staticmethod
    def _validate_adsb_data(adsb):
        if not isinstance(adsb, dict):
            return False

        if "lat" in adsb:
            lat = adsb["lat"]
            if not isinstance(lat, (int, float)) or np.isnan(lat) or not (-90 <= lat <= 90):
                return False

        if "lon" in adsb:
            lon = adsb["lon"]
            if not isinstance(lon, (int, float)) or np.isnan(lon) or not (-180 <= lon <= 180):
                return False

        if "alt_baro" in adsb:
            alt = adsb["alt_baro"]
            if not isinstance(alt, (int, float)) or np.isnan(alt) or not (-1000 <= alt <= 60000):
                return False

        if "gs" in adsb:
            gs = adsb["gs"]
            if not isinstance(gs, (int, float)) or np.isnan(gs) or gs < 0:
                return False

        if "track" in adsb:
            track = adsb["track"]
            if not isinstance(track, (int, float)) or np.isnan(track) or not (0 <= track < 360):
                return False

        return True

    def _check_doppler_anomaly(self, detection):
        doppler = abs(detection["doppler"])
        threshold = get_mach1_doppler_threshold()

        fc = _get_param("radar", "center_frequency", 200000000)
        velocity_ms = doppler * SPEED_OF_LIGHT / (2 * fc)

        if velocity_ms > self.max_velocity_ms:
            self.max_velocity_ms = velocity_ms

        if doppler < threshold:
            return False

        if detection.get("adsb"):
            adsb_gs = detection["adsb"].get("gs")
            if adsb_gs is not None and isinstance(adsb_gs, (int, float)) and not np.isnan(adsb_gs):
                adsb_velocity_ms = adsb_gs * KNOTS_TO_MS
                if adsb_velocity_ms >= MACH_1_MS:
                    return False

        self.is_anomalous = True
        self.anomaly_types.add("supersonic")
        return True

    def _check_velocity_anomaly(self, detection, timestamp):
        if not detection.get("adsb"):
            return False

        adsb = detection["adsb"]
        gs = adsb.get("gs")

        if gs is None or not isinstance(gs, (int, float)) or np.isnan(gs):
            return False

        velocity_ms = geometry.knots_to_ms(gs)

        if velocity_ms > self.max_velocity_ms:
            self.max_velocity_ms = velocity_ms

        if velocity_ms > MACH_1_MS:
            self.is_anomalous = True
            self.anomaly_types.add("supersonic")
            self.anomaly_detections.append(
                {
                    "timestamp": timestamp,
                    "type": "supersonic",
                    "velocity_ms": velocity_ms,
                    "velocity_knots": gs,
                    "mach": velocity_ms / MACH_1_MS,
                }
            )
            return True

        return False

    def _check_acceleration_anomaly(self, detection, timestamp):
        if not detection.get("adsb"):
            return False

        adsb = detection["adsb"]
        gs = adsb.get("gs")

        if gs is None or not isinstance(gs, (int, float)) or np.isnan(gs):
            return False

        velocity_ms = geometry.knots_to_ms(gs)

        if self.last_velocity_ms is not None and len(self.history["timestamps"]) > 1:
            dt = (timestamp - self.history["timestamps"][-2]) / 1000.0

            if dt > 0 and dt < 10.0:
                dv = abs(velocity_ms - self.last_velocity_ms)
                acceleration = dv / dt

                if acceleration > MAX_NORMAL_ACCEL_MS2:
                    self.is_anomalous = True
                    self.anomaly_types.add("instant_acceleration")
                    self.anomaly_detections.append(
                        {
                            "timestamp": timestamp,
                            "type": "instant_acceleration",
                            "acceleration_ms2": acceleration,
                            "velocity_change_ms": dv,
                            "time_delta_sec": dt,
                        }
                    )
                    self.last_velocity_ms = velocity_ms
                    return True

        self.last_velocity_ms = velocity_ms
        return False

    def _check_direction_change_anomaly(self, detection, timestamp):
        if not detection.get("adsb"):
            return False

        adsb = detection["adsb"]
        track = adsb.get("track")

        if track is None or not isinstance(track, (int, float)) or np.isnan(track):
            return False

        if self.last_heading_deg is not None and len(self.history["timestamps"]) > 1:
            dt = (timestamp - self.history["timestamps"][-2]) / 1000.0

            if dt > 0 and dt < 10.0:
                dheading = abs(track - self.last_heading_deg)
                if dheading > 180:
                    dheading = 360 - dheading

                turn_rate = dheading / dt

                if turn_rate > MAX_DIRECTION_CHANGE_DEG_PER_SEC:
                    self.is_anomalous = True
                    self.anomaly_types.add("instant_direction_change")
                    self.anomaly_detections.append(
                        {
                            "timestamp": timestamp,
                            "type": "instant_direction_change",
                            "turn_rate_deg_per_sec": turn_rate,
                            "heading_change_deg": dheading,
                            "time_delta_sec": dt,
                        }
                    )
                    self.last_heading_deg = track
                    return True

        self.last_heading_deg = track
        return False

    def _check_sustained_turn_anomaly(self, detection, timestamp):
        """Detect sustained orbiting: continuous high turn rate over multiple frames."""
        if not detection.get("adsb"):
            return False
        track_hdg = detection["adsb"].get("track")
        if track_hdg is None or not isinstance(track_hdg, (int, float)) or np.isnan(track_hdg):
            return False
        if self.last_heading_deg is not None:
            delta = abs(track_hdg - self.last_heading_deg)
            if delta > 180:
                delta = 360 - delta
            self._heading_changes.append(delta)
            if len(self._heading_changes) > ORBIT_HEADING_WINDOW:
                self._heading_changes = self._heading_changes[-ORBIT_HEADING_WINDOW:]
            if len(self._heading_changes) >= ORBIT_HEADING_WINDOW:
                total = sum(self._heading_changes)
                if total >= ORBIT_MIN_CUMULATIVE_DEG:
                    self.is_anomalous = True
                    self.anomaly_types.add("sustained_orbit")
                    self.anomaly_detections.append({
                        "timestamp": timestamp,
                        "type": "sustained_orbit",
                        "cumulative_heading_change_deg": total,
                        "window_frames": len(self._heading_changes),
                    })
                    return True
        return False

    def _check_position_mismatch_anomaly(self, detection, timestamp):
        """Detect GPS spoofing: ADS-B position frozen while aircraft reports movement."""
        if not detection.get("adsb"):
            return False
        adsb = detection["adsb"]
        lat = adsb.get("lat")
        lon = adsb.get("lon")
        gs = adsb.get("gs")
        if lat is None or lon is None or gs is None:
            return False
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return False
        if np.isnan(lat) or np.isnan(lon):
            return False
        if self._last_adsb_lat is not None and self._last_adsb_lon is not None:
            dlat = abs(lat - self._last_adsb_lat)
            dlon = abs(lon - self._last_adsb_lon)
            if gs > SPOOF_MIN_SPEED_KTS and dlat < SPOOF_POSITION_EPSILON_DEG and dlon < SPOOF_POSITION_EPSILON_DEG:
                self._adsb_frozen_count += 1
            else:
                self._adsb_frozen_count = 0
            if self._adsb_frozen_count >= SPOOF_MIN_FROZEN_FRAMES:
                self.is_anomalous = True
                self.anomaly_types.add("position_mismatch")
                self.anomaly_detections.append({
                    "timestamp": timestamp,
                    "type": "position_mismatch",
                    "frozen_frames": self._adsb_frozen_count,
                    "reported_gs_kts": gs,
                    "position_delta_deg": max(dlat, dlon),
                })
                self._last_adsb_lat = lat
                self._last_adsb_lon = lon
                return True
        self._last_adsb_lat = lat
        self._last_adsb_lon = lon
        return False

    def _check_identity_change_anomaly(self, detection, timestamp):
        """Detect transponder identity swap: ADS-B hex changes on continuous track."""
        if not detection.get("adsb"):
            return False
        new_hex = detection["adsb"].get("hex")
        if not new_hex or not isinstance(new_hex, str):
            return False
        if self.adsb_hex is not None and new_hex.strip().lower() != self.adsb_hex.strip().lower():
            old_hex = self.adsb_hex
            self.is_anomalous = True
            self.anomaly_types.add("identity_swap")
            self.anomaly_detections.append({
                "timestamp": timestamp,
                "type": "identity_swap",
                "old_hex": old_hex,
                "new_hex": new_hex,
            })
            return True
        return False

    def _check_altitude_anomaly(self, detection, timestamp):
        """Detect impossible altitude jumps between consecutive frames."""
        if not detection.get("adsb"):
            return False
        alt_baro = detection["adsb"].get("alt_baro")
        if alt_baro is None or not isinstance(alt_baro, (int, float)) or np.isnan(alt_baro):
            return False
        if self._last_alt_baro is not None:
            dalt = abs(alt_baro - self._last_alt_baro)
            if dalt > ALTITUDE_JUMP_THRESHOLD_FT:
                self.is_anomalous = True
                self.anomaly_types.add("altitude_jump")
                self.anomaly_detections.append({
                    "timestamp": timestamp,
                    "type": "altitude_jump",
                    "altitude_change_ft": dalt,
                    "old_alt_ft": self._last_alt_baro,
                    "new_alt_ft": alt_baro,
                })
                self._last_alt_baro = alt_baro
                return True
        self._last_alt_baro = alt_baro
        return False

    def _check_anomalous_acceleration(self, detection, timestamp):
        """Detect extreme acceleration exceeding 10g (98.1 m/s²)."""
        if not detection.get("adsb"):
            return False

        adsb = detection["adsb"]
        gs = adsb.get("gs")

        if gs is None or not isinstance(gs, (int, float)) or np.isnan(gs):
            return False

        velocity_ms = geometry.knots_to_ms(gs)

        if self.last_velocity_ms is not None and len(self.history["timestamps"]) > 1:
            dt = (timestamp - self.history["timestamps"][-2]) / 1000.0

            if dt > 0 and dt < 120.0:
                dv = abs(velocity_ms - self.last_velocity_ms)
                acceleration = dv / dt

                if acceleration > ANOMALOUS_ACCEL_MS2:
                    self.is_anomalous = True
                    self.anomaly_types.add("anomalous_acceleration")
                    self.anomaly_detections.append({
                        "timestamp": timestamp,
                        "type": "anomalous_acceleration",
                        "acceleration_ms2": acceleration,
                        "acceleration_g": round(acceleration / 9.81, 1),
                        "velocity_change_ms": dv,
                        "time_delta_sec": dt,
                    })
                    return True

        return False

    def _check_long_hover(self, detection, timestamp):
        """Detect long hover: position unchanged for 15 min while detections arrive."""
        if not detection.get("adsb"):
            return False

        adsb = detection["adsb"]
        lat = adsb.get("lat")
        lon = adsb.get("lon")

        if lat is None or lon is None:
            return False
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return False
        if np.isnan(lat) or np.isnan(lon):
            return False

        if self._hover_anchor_lat is None:
            self._hover_anchor_lat = lat
            self._hover_anchor_lon = lon
            self._hover_start_ts = timestamp
            return False

        dlat = abs(lat - self._hover_anchor_lat)
        dlon = abs(lon - self._hover_anchor_lon)

        if dlat < LONG_HOVER_POSITION_EPSILON_DEG and dlon < LONG_HOVER_POSITION_EPSILON_DEG:
            duration_s = (timestamp - self._hover_start_ts) / 1000.0
            if duration_s >= LONG_HOVER_MIN_DURATION_S:
                self.is_anomalous = True
                self.anomaly_types.add("long_hover")
                self.anomaly_detections.append({
                    "timestamp": timestamp,
                    "type": "long_hover",
                    "hover_duration_s": round(duration_s, 1),
                    "anchor_lat": self._hover_anchor_lat,
                    "anchor_lon": self._hover_anchor_lon,
                })
                # Reset anchor to avoid repeated firing every frame
                self._hover_anchor_lat = lat
                self._hover_anchor_lon = lon
                self._hover_start_ts = timestamp
                return True
        else:
            # Position moved — reset anchor
            self._hover_anchor_lat = lat
            self._hover_anchor_lon = lon
            self._hover_start_ts = timestamp

        return False

    def _init_from_adsb(self, detection, adsb_config):
        adsb = detection["adsb"]

        if not self._validate_adsb_data(adsb):
            self._init_from_delay_doppler(detection)
            return

        self.adsb_hex = adsb.get("hex")
        self.adsb_initialized = True

        self.state = np.array([detection["delay"], 0.0, detection["doppler"], 0.0])

        if adsb.get("gs") is not None and adsb.get("track") is not None:
            gs = adsb["gs"]
            track = adsb["track"]
            if not (gs >= 0 and 0 <= track < 360 and not np.isnan(gs) and not np.isnan(track)):
                pass
            else:
                vel_east, vel_north, vel_up = geometry.enu_velocity_from_adsb(gs, track, adsb.get("geom_rate", 0))
                if np.isnan(vel_east) or np.isnan(vel_north) or np.isnan(vel_up):
                    pass
                else:
                    vel_horiz = np.sqrt(vel_east**2 + vel_north**2)
                    if np.isnan(vel_horiz) or np.isinf(vel_horiz):
                        pass
                    else:
                        delay_rate_est = vel_horiz / 299792.458
                        if not (np.isnan(delay_rate_est) or np.isinf(delay_rate_est)):
                            self.state[1] = delay_rate_est

        pos_unc = adsb_config["initial_covariance"]["position"]
        vel_unc = adsb_config["initial_covariance"]["velocity"]
        delay_unc = pos_unc / 1000.0
        self.covariance = np.diag([delay_unc, vel_unc / 1000, 20.0, 10.0])

    def _init_from_delay_doppler(self, detection):
        self.state = np.array([detection["delay"], 0.0, detection["doppler"], 0.0])
        self.covariance = np.diag([10.0, 5.0, 20.0, 10.0])

    @classmethod
    def _generate_id(cls, timestamp_ms, adsb_hex=None):
        dt = datetime.fromtimestamp(timestamp_ms / 1000.0)
        date_str = dt.strftime("%y%m%d")

        if adsb_hex:
            return f"{date_str}-{adsb_hex.upper()}"

        if cls._last_date != date_str:
            cls._daily_counter = 0
            cls._last_date = date_str

        track_id = f"{date_str}-{cls._daily_counter:06X}"
        cls._daily_counter += 1
        return track_id

    def predict(self, dt):
        self.kf.dt = dt
        self.state, self.covariance = self.kf.predict(self.state, self.covariance)

    def update(self, detection, timestamp, frame=0):
        measurement = np.array([detection["delay"], detection["doppler"]])
        self.state, self.covariance = self.kf.update(self.state, self.covariance, measurement, detection.get("snr"))

        # Identity swap check MUST run before adsb_hex capture
        self._check_identity_change_anomaly(detection, timestamp)

        # Capture ADS-B hex on first association that carries one.  Tracks
        # initialised from clutter miss this in __init__; this back-fills it.
        _det_adsb = detection.get("adsb")
        if self.adsb_hex is None and _det_adsb:
            adsb = _det_adsb
            if self._validate_adsb_data(adsb) and adsb.get("hex"):
                self.adsb_hex = adsb["hex"]
                self.adsb_initialized = True

        self.history["timestamps"].append(timestamp)
        self.history["frames"].append(frame)
        self.history["states"].append(self.state.copy())
        self.history["measurements"].append(detection)
        self.history["state_status"].append(self.state_status.name)

        self.n_associated += 1
        self.n_missed = 0
        self.n_frames += 1

        self.total_snr += detection.get("snr", 0)
        self.death_timestamp = timestamp

        self._check_velocity_anomaly(detection, timestamp)
        self._check_doppler_anomaly(detection)
        self._check_acceleration_anomaly(detection, timestamp)
        self._check_sustained_turn_anomaly(detection, timestamp)
        self._check_direction_change_anomaly(detection, timestamp)
        self._check_position_mismatch_anomaly(detection, timestamp)
        self._check_altitude_anomaly(detection, timestamp)
        self._check_anomalous_acceleration(detection, timestamp)
        self._check_long_hover(detection, timestamp)

    def mark_missed(self, timestamp, frame=0):
        """Mark track as not associated this frame."""
        self.n_missed += 1
        self.n_frames += 1

        self.history["timestamps"].append(timestamp)
        self.history["frames"].append(frame)
        self.history["states"].append(self.state.copy())
        self.history["measurements"].append(None)
        self.history["state_status"].append(self.state_status.name)

        self.death_timestamp = timestamp

    def get_predicted_measurement(self):
        return self.kf.H @ self.state

    def get_innovation_covariance(self):
        return self.kf.get_innovation_covariance(self.covariance)

    def promote_if_ready(self):
        if self.state_status == TrackState.TENTATIVE:
            if self.n_frames >= N_WINDOW():
                if self.n_associated >= M_THRESHOLD():
                    self.state_status = TrackState.ACTIVE
                    return True
        return False

    def get_quality_score(self):
        if self.n_frames == 0:
            return 0.0

        continuity = (self.n_associated / self.n_frames) * 40.0
        avg_snr = self.total_snr / max(self.n_associated, 1)
        snr_score = min((avg_snr / 15.0) * 30.0, 30.0)
        duration_sec = (self.death_timestamp - self.birth_timestamp) / 1000.0
        duration_score = min((duration_sec / 60.0) * 20.0, 20.0)
        assoc_score = min((self.n_associated / 50.0) * 10.0, 10.0)

        return continuity + snr_score + duration_score + assoc_score

    def is_high_quality(self):
        if self.n_associated < 3:
            return False

        continuity = self.n_associated / max(self.n_frames, 1)
        if continuity < 0.4:
            return False

        avg_snr = self.total_snr / max(self.n_associated, 1)
        if avg_snr < 8.0:
            return False

        duration_sec = (self.death_timestamp - self.birth_timestamp) / 1000.0
        if duration_sec < 5.0:
            return False

        return True

    def should_delete(self):
        if self.state_status == TrackState.TENTATIVE:
            return self.n_frames > N_WINDOW()
        else:
            return self.n_missed > N_DELETE()

    def get_length_bucket(self):
        if self.n_associated < 10:
            return "short"
        elif self.n_associated < 50:
            return "medium"
        else:
            return "long"

    def get_recent_detections(self, n=20):
        """Return the last *n* non-None detections (reverse scan, early exit)."""
        measurements = self.history["measurements"]
        timestamps = self.history["timestamps"]
        result = []
        for i in range(len(measurements) - 1, -1, -1):
            m = measurements[i]
            if m is not None:
                result.append(
                    {
                        "timestamp": timestamps[i],
                        "delay": m["delay"],
                        "doppler": m["doppler"],
                        "snr": m["snr"],
                        "adsb": m.get("adsb"),
                    }
                )
                if len(result) >= n:
                    break
        result.reverse()
        return result

    def to_dict(self):
        duration_sec = (self.death_timestamp - self.birth_timestamp) / 1000.0
        avg_snr = self.total_snr / max(self.n_associated, 1)
        continuity = self.n_associated / max(self.n_frames, 1)

        return {
            "id": self.id,
            "adsb_hex": self.adsb_hex,
            "adsb_initialized": self.adsb_initialized,
            "state_status": self.state_status.name,
            "n_frames": self.n_frames,
            "n_associated": self.n_associated,
            "length_bucket": self.get_length_bucket(),
            "quality_score": self.get_quality_score(),
            "avg_snr": avg_snr,
            "duration_sec": duration_sec,
            "continuity": continuity,
            "birth_timestamp": self.birth_timestamp,
            "death_timestamp": self.death_timestamp,
            "is_anomalous": self.is_anomalous,
            "max_velocity_ms": self.max_velocity_ms,
            "anomaly_types": list(self.anomaly_types),
            "anomaly_detections": self.anomaly_detections,
            "history": {
                "timestamps": self.history["timestamps"],
                "states": [s.tolist() for s in self.history["states"]],
                "delays": [m["delay"] if m else None for m in self.history["measurements"]],
                "dopplers": [m["doppler"] if m else None for m in self.history["measurements"]],
                "snrs": [m["snr"] if m else None for m in self.history["measurements"]],
                "state_status": self.history["state_status"],
            },
        }
