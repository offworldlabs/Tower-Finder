"""TrackEventWriter for streaming JSONL output."""

import json
import sys


class TrackEventWriter:
    """Writes track lifecycle events in JSONL (JSON Lines) format.

    Each event is a single JSON object on its own line, enabling streaming consumption.
    """

    def __init__(self, output_file):
        if output_file == "-":
            self.output = sys.stdout
            self._is_stdout = True
        else:
            self.output = open(output_file, "w")
            self._is_stdout = False

    def write_event(
        self,
        track_id,
        timestamp,
        length,
        detections,
        adsb_hex=None,
        adsb_initialized=False,
        is_anomalous=False,
        max_velocity_ms=0.0,
        anomaly_types=None,
    ):
        event = {
            "track_id": track_id,
            "adsb_hex": adsb_hex,
            "adsb_initialized": adsb_initialized,
            "timestamp": timestamp,
            "length": length,
            "detections": detections,
            "is_anomalous": is_anomalous,
            "max_velocity_ms": max_velocity_ms,
            "anomaly_types": sorted(anomaly_types) if anomaly_types else [],
        }
        self.output.write(json.dumps(event) + "\n")
        self.output.flush()

    def close(self):
        if not self._is_stdout:
            self.output.close()
