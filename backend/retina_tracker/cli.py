"""CLI entry point and file processing for retina-tracker."""

import argparse
import json
import sys

import numpy as np

from .config import (
    get_config,
    load_blah2_config,
    load_config,
    set_config,
)
from .output import TrackEventWriter
from .server import run_tcp_server
from .tracker import Tracker


def load_detections(filepath):
    """Load detection data from JSON or JSONL file."""
    with open(filepath, "r") as f:
        content = f.read().strip()

        try:
            detections = json.loads(content)
            if isinstance(detections, list):
                return detections
        except json.JSONDecodeError:
            pass

        detections = []
        lines = content.split("\n")
        for line_num, line in enumerate(lines, start=1):
            line = line.strip()
            if line:
                try:
                    detections.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"Warning: Failed to parse line {line_num} in {filepath}: {e}", file=sys.stderr)
                    print(f"  Line content: {line[:100]}{'...' if len(line) > 100 else ''}", file=sys.stderr)

        return detections

    return []


def process_detections(detections_file, event_writer=None, detection_window=20):
    """Process all detections and generate tracks."""
    output = sys.stderr if event_writer and event_writer._is_stdout else sys.stdout

    print(f"Loading detections from {detections_file}...", file=output)
    detection_frames = load_detections(detections_file)
    print(f"Loaded {len(detection_frames)} detection frames", file=output)

    tracker = Tracker(event_writer=event_writer, detection_window=detection_window, config=get_config())

    for i, frame in enumerate(detection_frames):
        timestamp = frame["timestamp"]
        delays = frame.get("delay", [])
        dopplers = frame.get("doppler", [])
        snrs = frame.get("snr", [])
        adsb_list = frame.get("adsb", [])

        detections = []
        for idx, (delay, doppler, snr) in enumerate(zip(delays, dopplers, snrs)):
            detection = {"delay": delay, "doppler": doppler, "snr": snr}
            if adsb_list and idx < len(adsb_list) and adsb_list[idx] is not None:
                detection["adsb"] = adsb_list[idx]
            detections.append(detection)

        tracker.process_frame(detections, timestamp)

        if (i + 1) % 100 == 0:
            print(
                f"Processed {i + 1}/{len(detection_frames)} frames, "
                f"{len(tracker.tracks)} tracks ({len(tracker.get_active_tracks())} active)",
                file=output,
            )

    return tracker


def save_tracks(tracker, output_file):
    """Save tracks to JSON file."""
    data = tracker.to_dict()
    with open(output_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {data['n_tracks']} tracks to {output_file}", file=sys.stderr)


def visualize_tracks(
    tracker, detections_file, output_image, tracks_only=False, min_associations=0, length_bucket="all"
):
    """Visualize tracks overlaid on detections or tracks only."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 10))

    if not tracks_only:
        detection_frames = load_detections(detections_file)
        all_delays = []
        all_dopplers = []
        all_snrs = []

        for frame in detection_frames:
            all_delays.extend(frame.get("delay", []))
            all_dopplers.extend(frame.get("doppler", []))
            all_snrs.extend(frame.get("snr", []))

    colors = plt.cm.tab20(np.linspace(0, 1, 20))

    plot_tracks = tracker.get_confirmed_tracks()

    if min_associations > 0:
        plot_tracks = [t for t in plot_tracks if t.n_associated >= min_associations]

    if length_bucket != "all":
        plot_tracks = [t for t in plot_tracks if t.get_length_bucket() == length_bucket]

    print(
        f"Plotting {len(plot_tracks)} tracks (min_assoc >= {min_associations}, length_bucket = {length_bucket})",
        file=sys.stderr,
    )

    for i, track in enumerate(plot_tracks):
        color = colors[i % 20]

        measurements = track.history["measurements"]
        delays = [m["delay"] for m in measurements if m is not None]
        dopplers = [m["doppler"] for m in measurements if m is not None]

        if len(delays) == 0:
            continue

        ax.plot(delays, dopplers, "-", color="black", linewidth=0.5, alpha=0.3, zorder=1)

        ax.scatter(
            delays,
            dopplers,
            c=[color] * len(delays),
            s=35,
            alpha=0.8,
            edgecolors="none",
            zorder=2,
            label=f"Track {track.id}",
        )

    if not tracks_only:
        scatter = ax.scatter(
            all_delays, all_dopplers, c=all_snrs, cmap="coolwarm", s=5, alpha=0.4, zorder=3, label="Detections"
        )
        plt.colorbar(scatter, ax=ax, label="SNR (dB)")

    ax.set_xlabel("Delay", fontsize=12)
    ax.set_ylabel("Doppler (Hz)", fontsize=12)
    title = "Radar Tracks Only" if tracks_only else "Radar Tracks"
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.grid(True, alpha=0.3)

    if len(plot_tracks) > 0:
        if len(plot_tracks) <= 15:
            ax.legend(loc="upper right", fontsize=8, ncol=2)
        else:
            handles, labels = ax.get_legend_handles_labels()
            ax.legend(handles[:15], labels[:15], loc="upper right", fontsize=8, ncol=2)

    plt.tight_layout()
    fig.savefig(output_image, dpi=150, bbox_inches="tight")
    print(f"Saved visualization to {output_image}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Track bistatic radar detections")
    parser.add_argument("file", nargs="?", help="Path to .detection file (omit for --tcp mode)")
    parser.add_argument("-o", "--output", default="tracks.json", help="Output JSON file for tracks")
    parser.add_argument("-v", "--visualize", help="Output image file for visualization")
    parser.add_argument("--tracks-only", action="store_true", help="Visualize tracks only (no detection background)")
    parser.add_argument("--min-assoc", type=int, default=0, help="Minimum associations to plot a track (default: 0)")
    parser.add_argument(
        "--length-bucket",
        choices=["short", "medium", "long", "all"],
        default="all",
        help="Filter tracks by length bucket (short:<10, medium:10-49, long:>=50)",
    )
    parser.add_argument(
        "-s", "--stream-output", type=str, help="Output file for streaming JSONL events (use - for stdout)"
    )
    parser.add_argument(
        "--detection-window",
        type=int,
        default=20,
        help="Number of detections to include in sliding window (default: 20)",
    )
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file (default: config.yaml)")
    parser.add_argument("--blah2-config", type=str, help="Path to blah2 config.yml to read center frequency (fc)")

    parser.add_argument("--tcp", action="store_true", help="Run as TCP server for streaming input from blah2")
    parser.add_argument("--tcp-host", default="0.0.0.0", help="TCP bind address (default: 0.0.0.0)")
    parser.add_argument("--tcp-port", type=int, default=3012, help="TCP port to listen on (default: 3012)")

    args = parser.parse_args()

    if not args.file and not args.tcp:
        parser.error("Either provide a detection file or use --tcp for streaming mode")
    if args.file and args.tcp:
        parser.error("Cannot use both file input and --tcp mode")

    if args.config:
        set_config(load_config(args.config))

    if args.blah2_config:
        fc = load_blah2_config(args.blah2_config)
        if fc is not None:
            config = get_config()
            config["radar"]["center_frequency"] = fc

    event_writer = None
    if args.stream_output:
        event_writer = TrackEventWriter(args.stream_output)
    elif args.tcp:
        event_writer = TrackEventWriter("-")

    if args.tcp:
        run_tcp_server(
            host=args.tcp_host,
            port=args.tcp_port,
            event_writer=event_writer,
            detection_window=args.detection_window,
            config=get_config(),
        )
    else:
        tracker = process_detections(args.file, event_writer=event_writer, detection_window=args.detection_window)

        if event_writer:
            event_writer.close()

        save_tracks(tracker, args.output)

        if args.visualize:
            visualize_tracks(
                tracker,
                args.file,
                args.visualize,
                tracks_only=args.tracks_only,
                min_associations=args.min_assoc,
                length_bucket=args.length_bucket,
            )
