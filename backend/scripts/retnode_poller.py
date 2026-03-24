#!/usr/bin/env python3
"""
retnode_poller.py — Live-node bridge for RETINA passive radar network.

Polls the blah2 HTTP API on a real retnode (e.g. radar3.retnode.com) and
feeds detection frames into the RETINA tracker backend over the standard
TCP protocol, so the live node appears as a connected node in the dashboard.

This is an interim approach until nodes connect directly via mTLS.

Usage:
    # Against local dev server
    python retnode_poller.py

    # Against production server
    python retnode_poller.py --server 157.245.214.30 --port 3012

    # Custom node URL / ID
    python retnode_poller.py \\
        --node-url http://radar3.retnode.com \\
        --node-id radar3 \\
        --server localhost --port 3012

    # Override reference transmitter location
    python retnode_poller.py --tx-lat 33.75667 --tx-lon -84.331844 --tx-alt-ft 1600

Config is automatically fetched from <node-url>/api/config on startup.
Detections are polled from <node-url>/api/detection at --poll-interval seconds.
Duplicate frames (same timestamp) are silently skipped.
"""

import argparse
import hashlib
import json
import select
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    import urllib.request as _urllib_request
    _HAS_REQUESTS = False

_UA = "retnode-poller/1.0"

# ── Constants ─────────────────────────────────────────────────────────────────

RETINA_VERSION = "1.0"
HEARTBEAT_INTERVAL_S = 30
CONFIG_ACK_TIMEOUT_S = 10
FT_PER_M = 3.28084

# Default TX: reference FM broadcast transmitter (matches simulation default)
DEFAULT_TX_LAT = 33.75667
DEFAULT_TX_LON = -84.331844
DEFAULT_TX_ALT_FT = 1600.0

# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _http_get(url: str, timeout: float = 5.0) -> Optional[dict]:
    """Fetch JSON from a URL. Returns None on any error."""
    try:
        if _HAS_REQUESTS:
            r = _requests.get(url, timeout=timeout, headers={"User-Agent": _UA})
            r.raise_for_status()
            return r.json()
        else:
            req = _urllib_request.Request(url, headers={"User-Agent": _UA})
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  ! HTTP GET {url} failed: {exc}", file=sys.stderr)
        return None


# ── Config mapping ────────────────────────────────────────────────────────────


def _build_retina_config(
    raw_cfg: dict,
    node_id: str,
    tx_lat: float,
    tx_lon: float,
    tx_alt_ft: float,
) -> dict:
    """Map a blah2 /api/config payload to the RETINA NodeConfig dict.

    The returned dict uses the field names expected by:
      - NodeAnalyticsManager.register_node()
      - InterNodeAssociator.register_node()
      - PassiveRadarPipeline
    """
    capture = raw_cfg.get("capture", {})
    tar1090 = raw_cfg.get("tar1090", {})
    proc = raw_cfg.get("process", {})
    ambiguity = proc.get("ambiguity", {})
    detection_cfg = proc.get("detection", {})

    # blah2 v2 config has a top-level `location` dict with `rx` and `tx` sub-
    # keys containing accurate lat/lon/altitude for both ends of the bistatic
    # baseline.  Fall back to the older `tar1090.location` (RX only) for
    # backward compatibility.
    loc_top = raw_cfg.get("location", {})
    rx_loc = loc_top.get("rx") or tar1090.get("location", {})
    tx_loc = loc_top.get("tx")  # None if not present in config

    rx_alt_m = float(rx_loc.get("altitude", 0))

    # TX: prefer config values; fall back to CLI/default args.
    if tx_loc:
        resolved_tx_lat = float(tx_loc.get("latitude", tx_lat))
        resolved_tx_lon = float(tx_loc.get("longitude", tx_lon))
        resolved_tx_alt_ft = float(tx_loc.get("altitude", 0)) * FT_PER_M
    else:
        resolved_tx_lat = tx_lat
        resolved_tx_lon = tx_lon
        resolved_tx_alt_ft = tx_alt_ft

    return {
        "node_id": node_id,
        "rx_lat": float(rx_loc.get("latitude", 0.0)),
        "rx_lon": float(rx_loc.get("longitude", 0.0)),
        "rx_alt_ft": rx_alt_m * FT_PER_M,
        "tx_lat": resolved_tx_lat,
        "tx_lon": resolved_tx_lon,
        "tx_alt_ft": resolved_tx_alt_ft,
        "fc_hz": float(capture.get("fc", 195_000_000)),
        "fs_hz": float(capture.get("fs", 2_000_000)),
        "doppler_min": float(ambiguity.get("dopplerMin", -300)),
        "doppler_max": float(ambiguity.get("dopplerMax", 300)),
        "min_doppler": float(detection_cfg.get("minDoppler", 15)),
        # Preserve original blah2 config for reference / display
        "source_url": "https://radar3.retnode.com",
        "source_config": raw_cfg,
    }


def _config_hash(cfg: dict) -> str:
    """Short deterministic hash of config (excludes display-only fields)."""
    stable = {
        k: v for k, v in cfg.items()
        if k not in ("source_url", "source_config")
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode()
    ).hexdigest()[:16]


# ── TCP helpers ───────────────────────────────────────────────────────────────


def _tcp_connect(host: str, port: int) -> socket.socket:
    """Connect to the RETINA TCP server with exponential-backoff retries."""
    attempt = 0
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            sock.settimeout(None)
            print(f"[poller] TCP connected to {host}:{port}", file=sys.stderr)
            return sock
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            attempt += 1
            wait = min(2 ** attempt, 30)
            print(
                f"[poller] TCP connect failed ({exc}), retrying in {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)


_sock_lock: threading.Lock = threading.Lock()


def _send_msg(sock: socket.socket, msg: dict) -> None:
    """Send a newline-delimited JSON message. Thread-safe via _sock_lock."""
    data = (json.dumps(msg) + "\n").encode("utf-8")
    with _sock_lock:
        sock.sendall(data)


def _recv_msg(sock: socket.socket, timeout: float = CONFIG_ACK_TIMEOUT_S) -> Optional[dict]:
    """Receive a single newline-delimited JSON message."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        sock.settimeout(None)
        return json.loads(buf.split(b"\n", 1)[0])
    except (socket.timeout, json.JSONDecodeError):
        sock.settimeout(None)
        return None


def _perform_handshake(sock: socket.socket, cfg: dict, cfg_hash: str) -> bool:
    """Perform RETINA HELLO → CONFIG → wait CONFIG_ACK handshake."""
    node_id = cfg["node_id"]

    _send_msg(sock, {
        "type": "HELLO",
        "node_id": node_id,
        "version": RETINA_VERSION,
        "is_synthetic": False,
        "capabilities": {
            "detection": True,
            "adsb_correlation": True,
            "doppler": True,
            "config_hash": True,
            "heartbeat": True,
            "chain_of_custody": False,
        },
    })
    print(f"[poller]   → HELLO (node_id={node_id})", file=sys.stderr)

    _send_msg(sock, {
        "type": "CONFIG",
        "node_id": node_id,
        "config_hash": cfg_hash,
        "config": cfg,
        "is_synthetic": False,
    })
    print(f"[poller]   → CONFIG (hash={cfg_hash})", file=sys.stderr)

    for attempt in range(3):
        ack = _recv_msg(sock, timeout=CONFIG_ACK_TIMEOUT_S)
        if ack and ack.get("type") == "CONFIG_ACK":
            if ack.get("config_hash") == cfg_hash:
                print(f"[poller]   ← CONFIG_ACK — handshake complete", file=sys.stderr)
                return True
            print(f"[poller]   ← CONFIG_ACK hash mismatch (expected={cfg_hash})", file=sys.stderr)
        elif ack:
            print(f"[poller]   ← unexpected: {ack.get('type', '?')}", file=sys.stderr)
        else:
            print(
                f"[poller]   ! CONFIG_ACK timeout (attempt {attempt + 1}/3), resending...",
                file=sys.stderr,
            )
            _send_msg(sock, {
                "type": "CONFIG",
                "node_id": node_id,
                "config_hash": cfg_hash,
                "config": cfg,
            })

    print("[poller]   ! Handshake failed after 3 attempts", file=sys.stderr)
    return False


# ── Background threads ────────────────────────────────────────────────────────


def _heartbeat_loop(
    sock: socket.socket,
    node_id: str,
    cfg_hash: str,
    stop_event: threading.Event,
) -> None:
    """Send periodic HEARTBEAT messages on a background thread."""
    print(f"[poller] heartbeat thread started (interval={HEARTBEAT_INTERVAL_S}s)", file=sys.stderr)
    while not stop_event.wait(HEARTBEAT_INTERVAL_S):
        try:
            _send_msg(sock, {
                "type": "HEARTBEAT",
                "node_id": node_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "config_hash": cfg_hash,
                "status": "active",
            })
            print(f"[poller]   → HEARTBEAT sent", file=sys.stderr)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            print(f"[poller] heartbeat error: {exc}", file=sys.stderr)
            break
    print(f"[poller] heartbeat thread stopped", file=sys.stderr)


def _listener_loop(
    sock: socket.socket,
    node_id: str,
    cfg: dict,
    cfg_hash: str,
    stop_event: threading.Event,
) -> None:
    """Handle server-initiated messages (CONFIG_REQUEST, etc.)."""
    # Use select() with a 1s timeout instead of sock.settimeout() so we don't
    # interfere with sendall() calls on the same socket from other threads.
    buf = b""
    while not stop_event.is_set():
        try:
            ready, _, _ = select.select([sock], [], [], 1.0)
            if not ready:
                continue
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "CONFIG_REQUEST":
                    print(f"[poller]   ← CONFIG_REQUEST — resending config", file=sys.stderr)
                    try:
                        _send_msg(sock, {
                            "type": "CONFIG",
                            "node_id": node_id,
                            "config_hash": cfg_hash,
                            "config": cfg,
                        })
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        except (ConnectionResetError, OSError):
            break


# ── Main polling loop ─────────────────────────────────────────────────────────


def run_poller(
    node_url: str,
    node_id: str,
    server_host: str,
    server_port: int,
    poll_interval_s: float,
    tx_lat: float,
    tx_lon: float,
    tx_alt_ft: float,
) -> None:
    print(f"[poller] node_url={node_url}  node_id={node_id}", file=sys.stderr)
    print(f"[poller] server={server_host}:{server_port}", file=sys.stderr)
    print(f"[poller] poll_interval={poll_interval_s}s", file=sys.stderr)

    # Fetch and translate node config (retry until available)
    raw_cfg: Optional[dict] = None
    while raw_cfg is None:
        print("[poller] Fetching node config...", file=sys.stderr)
        raw_cfg = _http_get(f"{node_url}/api/config")
        if raw_cfg is None:
            print("[poller] Config not available, retrying in 5s...", file=sys.stderr)
            time.sleep(5)

    cfg = _build_retina_config(raw_cfg, node_id, tx_lat, tx_lon, tx_alt_ft)
    cfg_hash = _config_hash(cfg)
    print(
        f"[poller] Config ready: "
        f"rx=({cfg['rx_lat']:.5f}, {cfg['rx_lon']:.5f})  "
        f"tx=({cfg['tx_lat']:.5f}, {cfg['tx_lon']:.5f})  "
        f"fc={cfg['fc_hz'] / 1e6:.1f} MHz  hash={cfg_hash}",
        file=sys.stderr,
    )

    last_timestamp: Optional[int] = None
    frames_sent = 0
    frames_skipped = 0

    while True:
        sock = _tcp_connect(server_host, server_port)

        if not _perform_handshake(sock, cfg, cfg_hash):
            print("[poller] Handshake failed, reconnecting in 5s...", file=sys.stderr)
            sock.close()
            time.sleep(5)
            continue

        stop_event = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(sock, node_id, cfg_hash, stop_event),
            daemon=True,
        )
        hb_thread.start()
        listener_thread = threading.Thread(
            target=_listener_loop,
            args=(sock, node_id, cfg, cfg_hash, stop_event),
            daemon=True,
        )
        listener_thread.start()

        print(
            f"[poller] Polling {node_url}/api/detection every {poll_interval_s}s...",
            file=sys.stderr,
        )

        try:
            while True:
                detection = _http_get(f"{node_url}/api/detection")
                if detection is None:
                    time.sleep(poll_interval_s)
                    continue

                ts = detection.get("timestamp")

                # Skip duplicate frames — the node serves the most recent
                # detection continuously, so many polls will return the same ts.
                if ts is not None and ts == last_timestamp:
                    frames_skipped += 1
                    time.sleep(poll_interval_s)
                    continue

                last_timestamp = ts

                _send_msg(sock, {"type": "DETECTION", "data": detection})
                frames_sent += 1

                if frames_sent == 1 or frames_sent % 100 == 0:
                    print(
                        f"[poller] sent={frames_sent} skipped={frames_skipped} "
                        f"detections={len(detection.get('delay', []))} ts={ts}",
                        file=sys.stderr,
                    )

                time.sleep(poll_interval_s)

        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            print(f"[poller] TCP error: {exc}", file=sys.stderr)
        finally:
            stop_event.set()
            try:
                sock.close()
            except OSError:
                pass

        print("[poller] Reconnecting in 3s...", file=sys.stderr)
        time.sleep(3)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Poll a live retnode HTTP API and bridge detections to the "
            "RETINA server via TCP.  Interim approach until mTLS is deployed."
        ),
    )
    parser.add_argument(
        "--node-url",
        default="https://radar3.retnode.com",
        help="Base URL of the retnode (default: https://radar3.retnode.com)",
    )
    parser.add_argument(
        "--node-id",
        default="radar3",
        help="Node ID to register in the RETINA server (default: radar3)",
    )
    parser.add_argument(
        "--server",
        default="localhost",
        help="RETINA server hostname or IP (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3012,
        help="RETINA server TCP port (default: 3012)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between detection polls (default: 0.5)",
    )
    parser.add_argument(
        "--tx-lat",
        type=float,
        default=DEFAULT_TX_LAT,
        help=f"Reference transmitter latitude (default: {DEFAULT_TX_LAT})",
    )
    parser.add_argument(
        "--tx-lon",
        type=float,
        default=DEFAULT_TX_LON,
        help=f"Reference transmitter longitude (default: {DEFAULT_TX_LON})",
    )
    parser.add_argument(
        "--tx-alt-ft",
        type=float,
        default=DEFAULT_TX_ALT_FT,
        help=f"Reference transmitter altitude in feet (default: {DEFAULT_TX_ALT_FT})",
    )
    args = parser.parse_args()

    run_poller(
        node_url=args.node_url.rstrip("/"),
        node_id=args.node_id,
        server_host=args.server,
        server_port=args.port,
        poll_interval_s=args.poll_interval,
        tx_lat=args.tx_lat,
        tx_lon=args.tx_lon,
        tx_alt_ft=args.tx_alt_ft,
    )


if __name__ == "__main__":
    main()
