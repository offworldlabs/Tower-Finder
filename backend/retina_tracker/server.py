"""TCP server for receiving detection frames from blah2."""

import json
import socket
import sys

from .config import get_config
from .tracker import Tracker


def process_streaming_frame(tracker, frame):
    """Convert blah2 streaming frame format to detections and process.

    Args:
        tracker: Tracker instance
        frame: Dict with 'timestamp', 'delay', 'doppler', 'snr', 'adsb' arrays
    """
    timestamp = frame["timestamp"]
    delays = frame.get("delay", [])
    dopplers = frame.get("doppler", [])
    snrs = frame.get("snr", [])
    adsb_list = frame.get("adsb", [])

    detections = []
    for idx, (delay, doppler, snr) in enumerate(zip(delays, dopplers, snrs)):
        detection = {
            "delay": delay,
            "doppler": doppler,
            "snr": snr,
        }
        if adsb_list and idx < len(adsb_list) and adsb_list[idx] is not None:
            detection["adsb"] = adsb_list[idx]
        detections.append(detection)

    tracker.process_frame(detections, timestamp)


def run_tcp_server(host="0.0.0.0", port=3012, event_writer=None, detection_window=20, config=None):
    """Run tracker as TCP server receiving detection frames from blah2.

    Args:
        host: Bind address (default: 0.0.0.0)
        port: TCP port to listen on (default: 3012)
        event_writer: TrackEventWriter for streaming output
        detection_window: Number of detections in sliding window
        config: Configuration dict
    """
    tracker = Tracker(
        event_writer=event_writer,
        detection_window=detection_window,
        config=config or get_config(),
    )

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)

    print(f"Tracker listening on {host}:{port}", file=sys.stderr)

    while True:
        conn, addr = server.accept()
        print(f"blah2 connected from {addr}", file=sys.stderr)

        buffer = ""
        while True:
            try:
                data = conn.recv(4096).decode("utf-8")
                if not data:
                    break

                buffer += data

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        frame = json.loads(line)
                        process_streaming_frame(tracker, frame)

            except (ConnectionResetError, BrokenPipeError):
                print("blah2 disconnected", file=sys.stderr)
                break
            except json.JSONDecodeError as e:
                print(f"JSON parse error: {e}", file=sys.stderr)
                continue

        conn.close()
        print("Waiting for blah2 reconnection...", file=sys.stderr)
