"""
Synthetic Node Microservice for Retina Passive Radar Network.

Generates and streams synthetic detection data over TCP to the
tracker server for testing. Implements the full RETINA node protocol:

  HELLO → CONFIG → REGISTER_KEY → (wait CONFIG_ACK, KEY_ACK)
  → steady state (HEARTBEAT + signed DETECTION + CHAIN_ENTRY)

Supports three detection modes:

1. Detection-only: delay/doppler/snr data (simulated aircraft tracks)
2. With ADS-B: some detections include ADS-B truth data
3. With anomalous objects: objects that have no ADS-B correlation

Chain of Custody:
  - Each detection is signed with P-256 ECDSA (software stub)
  - Hourly hash chain entries link all detections
  - TSA timestamps and OTS proofs anchor chain entries
  - IQ circular buffer runs for protocol testing

Usage:
    python synthetic_node.py                           # defaults
    python synthetic_node.py --host localhost --port 3012
    python synthetic_node.py --mode adsb               # include ADS-B
    python synthetic_node.py --mode anomalous          # include anomalous
    python synthetic_node.py --file data.detection     # replay file
    python synthetic_node.py --http http://localhost:8000/api/radar/detections

Node configuration (tower/receiver geometry) is loaded from
node_config.json or CLI arguments.
"""

import argparse
import hashlib
import json
import math
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# Speed of light km/μs
C_KM_US = 0.299792458

RETINA_VERSION = "1.0"
HEARTBEAT_INTERVAL_S = 60
CONFIG_ACK_TIMEOUT_S = 10


@dataclass
class NodeConfig:
    """Passive radar node configuration."""
    node_id: str = "synth-node-01"
    rx_lat: float = 33.939182
    rx_lon: float = -84.651910
    rx_alt_ft: float = 950.0
    tx_lat: float = 33.75667
    tx_lon: float = -84.331844
    tx_alt_ft: float = 1600.0
    fc_hz: float = 195_000_000.0
    fs_hz: float = 2_000_000.0
    doppler_min: float = -300.0
    doppler_max: float = 300.0
    min_doppler: float = 15.0


def _config_hash(config: NodeConfig) -> str:
    """Compute a short hash of the node configuration."""
    cfg_dict = asdict(config)
    cfg_str = json.dumps(cfg_dict, sort_keys=True)
    return hashlib.sha256(cfg_str.encode()).hexdigest()[:16]


@dataclass
class SyntheticTarget:
    """A simulated moving target in ENU coordinates (km) relative to RX."""
    target_id: str
    # Position (ENU km)
    east: float
    north: float
    up: float
    # Velocity (km/s)
    vel_east: float
    vel_north: float
    vel_up: float
    # Properties
    is_anomalous: bool = False
    adsb_hex: Optional[str] = None
    adsb_callsign: Optional[str] = None
    # Track lifetime
    created_at: float = 0.0
    lifetime_s: float = 300.0


def _lla_to_enu(lat, lon, alt_m, ref_lat, ref_lon, ref_alt_m):
    """Convert LLA to ENU (km) relative to reference point."""
    R = 6371.0
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    north = dlat * R
    east = dlon * R * math.cos(math.radians(ref_lat))
    up = (alt_m - ref_alt_m) / 1000.0
    return east, north, up


def _enu_to_lla(east_km, north_km, up_km, ref_lat, ref_lon, ref_alt_m):
    """Convert ENU (km) back to LLA."""
    R = 6371.0
    lat = ref_lat + math.degrees(north_km / R)
    lon = ref_lon + math.degrees(east_km / (R * math.cos(math.radians(ref_lat))))
    alt_m = ref_alt_m + up_km * 1000.0
    return lat, lon, alt_m


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _bistatic_delay(target_enu, tx_enu, rx_enu=(0, 0, 0)):
    """Compute bistatic differential delay in microseconds."""
    d_tx_tgt = _norm([target_enu[i] - tx_enu[i] for i in range(3)])
    d_tgt_rx = _norm([rx_enu[i] - target_enu[i] for i in range(3)])
    d_tx_rx = _norm([rx_enu[i] - tx_enu[i] for i in range(3)])
    diff_range = (d_tx_tgt + d_tgt_rx) - d_tx_rx
    return diff_range / C_KM_US


def _bistatic_doppler(target_enu, vel_enu, tx_enu, rx_enu, freq_hz):
    """Compute bistatic Doppler shift in Hz."""
    c_km_s = 299792.458

    # Unit vectors from target to TX and RX
    to_tx = [tx_enu[i] - target_enu[i] for i in range(3)]
    to_rx = [rx_enu[i] - target_enu[i] for i in range(3)]
    d_tx = _norm(to_tx)
    d_rx = _norm(to_rx)

    if d_tx < 1e-9 or d_rx < 1e-9:
        return 0.0

    u_tx = [to_tx[i] / d_tx for i in range(3)]
    u_rx = [to_rx[i] / d_rx for i in range(3)]

    # Radial velocities (positive = approaching)
    v_rad_tx = sum(vel_enu[i] * u_tx[i] for i in range(3))
    v_rad_rx = sum(vel_enu[i] * u_rx[i] for i in range(3))

    return (freq_hz / c_km_s) * (v_rad_tx + v_rad_rx)


class SyntheticNodeGenerator:
    """Generates realistic synthetic detection data."""

    def __init__(self, config: NodeConfig, mode: str = "detection"):
        self.config = config
        self.mode = mode  # "detection", "adsb", "anomalous"
        self.targets: list[SyntheticTarget] = []
        self._next_target_id = 1

        # Compute TX position in ENU (km) relative to RX
        rx_alt_m = config.rx_alt_ft * 0.3048
        tx_alt_m = config.tx_alt_ft * 0.3048
        self.rx_enu = (0.0, 0.0, 0.0)
        self.tx_enu = _lla_to_enu(
            config.tx_lat, config.tx_lon, tx_alt_m,
            config.rx_lat, config.rx_lon, rx_alt_m,
        )
        self.rx_alt_m = rx_alt_m

    def _spawn_target(self, now: float) -> SyntheticTarget:
        """Create a new synthetic aircraft target."""
        tid = f"synth-{self._next_target_id:04d}"
        self._next_target_id += 1

        # Random position 10-60 km from RX, 3-12 km altitude
        angle = random.uniform(0, 2 * math.pi)
        dist = random.uniform(10, 60)
        east = dist * math.cos(angle)
        north = dist * math.sin(angle)
        up = random.uniform(3, 12)  # 3-12 km altitude

        # Random velocity 100-250 m/s (typical aircraft)
        speed_km_s = random.uniform(0.1, 0.25)
        heading = random.uniform(0, 2 * math.pi)
        vel_east = speed_km_s * math.cos(heading)
        vel_north = speed_km_s * math.sin(heading)
        vel_up = random.uniform(-0.005, 0.005)  # slight climb/descent

        is_anomalous = False
        adsb_hex = None
        adsb_callsign = None

        if self.mode == "anomalous" and random.random() < 0.3:
            # 30% chance of anomalous target (no ADS-B, unusual behavior)
            is_anomalous = True
            # Anomalous targets can be slower/faster than normal aircraft
            speed_km_s = random.uniform(0.01, 0.4)
            vel_east = speed_km_s * math.cos(heading)
            vel_north = speed_km_s * math.sin(heading)
            up = random.uniform(0.3, 15)  # wider altitude range
        elif self.mode in ("adsb", "anomalous"):
            # Normal target with ADS-B in adsb/anomalous modes
            adsb_hex = f"{random.randint(0x100000, 0xFFFFFF):06x}"
            adsb_callsign = f"{''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=3))}{random.randint(100, 9999)}"

        return SyntheticTarget(
            target_id=tid,
            east=east,
            north=north,
            up=up,
            vel_east=vel_east,
            vel_north=vel_north,
            vel_up=vel_up,
            is_anomalous=is_anomalous,
            adsb_hex=adsb_hex,
            adsb_callsign=adsb_callsign,
            created_at=now,
            lifetime_s=random.uniform(120, 600),
        )

    def _update_target(self, target: SyntheticTarget, dt: float):
        """Update target position based on velocity."""
        target.east += target.vel_east * dt
        target.north += target.vel_north * dt
        target.up += target.vel_up * dt

        # Slight random velocity perturbation (maneuvers)
        target.vel_east += random.gauss(0, 0.001) * dt
        target.vel_north += random.gauss(0, 0.001) * dt

    def _target_detection(self, target: SyntheticTarget) -> dict:
        """Generate a detection measurement for a target."""
        pos = (target.east, target.north, target.up)
        vel = (target.vel_east, target.vel_north, target.vel_up)

        delay = _bistatic_delay(pos, self.tx_enu, self.rx_enu)
        doppler = _bistatic_doppler(
            pos, vel, self.tx_enu, self.rx_enu, self.config.fc_hz
        )

        # Add measurement noise
        delay += random.gauss(0, 0.3)  # ~0.3 μs noise
        doppler += random.gauss(0, 2.0)  # ~2 Hz noise

        # SNR depends on distance (closer = stronger)
        dist = _norm(pos)
        base_snr = 25 - 10 * math.log10(max(dist, 1))
        snr = max(base_snr + random.gauss(0, 2), 4.0)

        return {
            "delay": round(delay, 2),
            "doppler": round(doppler, 2),
            "snr": round(snr, 2),
            "_target": target,  # internal, stripped before output
        }

    def _make_adsb_entry(self, target: SyntheticTarget) -> Optional[dict]:
        """Generate ADS-B data for a target (if it has ADS-B and mode allows)."""
        if target.adsb_hex is None:
            return None
        if self.mode not in ("adsb", "anomalous"):
            return None

        # Convert ENU to LLA for ADS-B position
        lat, lon, alt_m = _enu_to_lla(
            target.east, target.north, target.up,
            self.config.rx_lat, self.config.rx_lon, self.rx_alt_m,
        )

        speed_ms = _norm([target.vel_east * 1000, target.vel_north * 1000, 0])
        track_deg = math.degrees(math.atan2(target.vel_east, target.vel_north)) % 360

        return {
            "hex": target.adsb_hex,
            "flight": target.adsb_callsign,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "alt_baro": round(alt_m / 0.3048),
            "gs": round(speed_ms * 1.94384, 1),
            "track": round(track_deg, 1),
        }

    def generate_frame(self, timestamp_ms: int) -> dict:
        """Generate a single detection frame.

        Returns a frame dict: {timestamp, delay[], doppler[], snr[], adsb?[]}
        """
        now = timestamp_ms / 1000.0

        # Manage target lifecycle
        # Remove expired targets
        self.targets = [
            t for t in self.targets
            if (now - t.created_at) < t.lifetime_s
        ]

        # Spawn new targets to maintain 3-8 active
        while len(self.targets) < 3:
            self.targets.append(self._spawn_target(now))
        if len(self.targets) < 8 and random.random() < 0.02:
            self.targets.append(self._spawn_target(now))

        # Update positions
        for target in self.targets:
            self._update_target(target, 0.5)  # ~0.5s between frames

        # Generate detections from all targets
        detections = []
        for target in self.targets:
            det = self._target_detection(target)
            detections.append(det)

        # Add some clutter/noise detections (false alarms)
        n_clutter = random.randint(0, 5)
        for _ in range(n_clutter):
            detections.append({
                "delay": round(random.uniform(0, 60), 2),
                "doppler": round(random.uniform(
                    self.config.doppler_min, self.config.doppler_max
                ), 2),
                "snr": round(random.uniform(4, 7), 2),
                "_target": None,
            })

        # Build output frame
        delays = [d["delay"] for d in detections]
        dopplers = [d["doppler"] for d in detections]
        snrs = [d["snr"] for d in detections]

        frame = {
            "timestamp": timestamp_ms,
            "delay": delays,
            "doppler": dopplers,
            "snr": snrs,
        }

        # Add ADS-B data in adsb/anomalous modes
        if self.mode in ("adsb", "anomalous"):
            adsb_list = []
            for det in detections:
                target = det.get("_target")
                if target is not None:
                    adsb_entry = self._make_adsb_entry(target)
                    adsb_list.append(adsb_entry)  # None for no-ADS-B targets
                else:
                    adsb_list.append(None)  # clutter has no ADS-B
            frame["adsb"] = adsb_list

        return frame


# ── TCP connection helpers ────────────────────────────────────────────────────

def _connect_tcp(host: str, port: int, max_retries: int = 0,
                 cloudflare_host: Optional[str] = None) -> socket.socket:
    """Connect to the tracker server via TCP with retry logic.

    If cloudflare_host is provided, the connection is made to the Cloudflare
    frontend (e.g. hub.re) so the server sees it as a real internet node.
    The actual TCP connection goes to cloudflare_host:port while the protocol
    identifies the node normally.
    """
    connect_host = cloudflare_host if cloudflare_host else host
    attempt = 0
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((connect_host, port))
            sock.settimeout(None)
            label = f"{connect_host}:{port}"
            if cloudflare_host:
                label += f" (via Cloudflare → {host})"
            print(f"Connected to {label}", file=sys.stderr)
            return sock
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            attempt += 1
            if 0 < max_retries <= attempt:
                raise
            wait = min(2 ** attempt, 30)
            print(
                f"Connection to {connect_host}:{port} failed ({exc}), "
                f"retrying in {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)


def _send_msg(sock: socket.socket, msg: dict):
    """Send a newline-delimited JSON message."""
    sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))


def _recv_msg(sock: socket.socket, timeout: float = CONFIG_ACK_TIMEOUT_S) -> Optional[dict]:
    """Receive a single newline-delimited JSON message with timeout."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        sock.settimeout(None)
        return json.loads(line)
    except (socket.timeout, json.JSONDecodeError):
        sock.settimeout(None)
        return None


# ── Protocol handshake ────────────────────────────────────────────────────────

def _perform_handshake(sock: socket.socket, config: NodeConfig,
                       crypto_backend=None) -> bool:
    """Perform the RETINA TCP handshake: HELLO → CONFIG → REGISTER_KEY → wait ACKs.

    If crypto_backend is provided, also registers the public key for
    chain of custody verification.
    Returns True if handshake succeeded, False otherwise.
    """
    cfg_hash = _config_hash(config)
    cfg_payload = asdict(config)

    is_synthetic = config.node_id.startswith("synth-") or config.node_id.startswith("syn-")

    # 1. Send HELLO with capabilities
    _send_msg(sock, {
        "type": "HELLO",
        "node_id": config.node_id,
        "version": RETINA_VERSION,
        "is_synthetic": is_synthetic,
        "capabilities": {
            "detection": True,
            "adsb_correlation": True,
            "doppler": True,
            "config_hash": True,
            "heartbeat": True,
            "chain_of_custody": crypto_backend is not None,
        },
    })
    print(f"  → HELLO (version={RETINA_VERSION}, synthetic={is_synthetic})", file=sys.stderr)

    # 2. Send CONFIG
    _send_msg(sock, {
        "type": "CONFIG",
        "node_id": config.node_id,
        "config_hash": cfg_hash,
        "config": cfg_payload,
    })
    print(f"  → CONFIG (hash={cfg_hash})", file=sys.stderr)

    # 3. Wait for CONFIG_ACK
    config_acked = False
    for attempt in range(3):
        ack = _recv_msg(sock, timeout=CONFIG_ACK_TIMEOUT_S)
        if ack and ack.get("type") == "CONFIG_ACK":
            ack_hash = ack.get("config_hash", "")
            if ack_hash == cfg_hash:
                print(f"  ← CONFIG_ACK (hash={ack_hash}) — handshake complete", file=sys.stderr)
                config_acked = True
                break
            else:
                print(f"  ← CONFIG_ACK hash mismatch (expected={cfg_hash} got={ack_hash})", file=sys.stderr)
        elif ack:
            print(f"  ← unexpected message: {ack.get('type', '?')}", file=sys.stderr)
        else:
            print(f"  ! CONFIG_ACK timeout (attempt {attempt + 1}/3), retransmitting CONFIG...", file=sys.stderr)
            _send_msg(sock, {
                "type": "CONFIG",
                "node_id": config.node_id,
                "config_hash": cfg_hash,
                "config": cfg_payload,
            })

    if not config_acked:
        print("  ! Handshake failed after 3 attempts", file=sys.stderr)
        return False

    # 4. Register public key (chain of custody)
    if crypto_backend is not None:
        _send_msg(sock, {
            "type": "REGISTER_KEY",
            "node_id": config.node_id,
            "public_key_pem": crypto_backend.get_public_key_pem(),
            "fingerprint": crypto_backend.get_public_key_fingerprint(),
            "serial_number": crypto_backend.get_serial_number(),
            "signing_mode": crypto_backend.signing_mode,
        })
        print(f"  → REGISTER_KEY (fp={crypto_backend.get_public_key_fingerprint()[:12]}...)", file=sys.stderr)
        # Wait for KEY_ACK (non-blocking — best effort)
        key_ack = _recv_msg(sock, timeout=5)
        if key_ack and key_ack.get("type") == "KEY_ACK":
            print(f"  ← KEY_ACK — key registered", file=sys.stderr)
        else:
            print(f"  ! KEY_ACK not received (chain of custody may not verify)", file=sys.stderr)

    return True


# ── Heartbeat thread ──────────────────────────────────────────────────────────

def _heartbeat_loop(sock: socket.socket, config: NodeConfig, stop_event: threading.Event):
    """Send periodic heartbeats on a background thread."""
    cfg_hash = _config_hash(config)
    while not stop_event.wait(HEARTBEAT_INTERVAL_S):
        try:
            _send_msg(sock, {
                "type": "HEARTBEAT",
                "node_id": config.node_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "config_hash": cfg_hash,
                "status": "active",
            })
        except (BrokenPipeError, ConnectionResetError, OSError):
            break  # main loop will handle reconnection


# ── Server message listener ──────────────────────────────────────────────────

def _listener_loop(sock: socket.socket, config: NodeConfig, stop_event: threading.Event):
    """Listen for server messages (CONFIG_REQUEST, etc.) on a background thread."""
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            for line in chunk.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "CONFIG_REQUEST":
                    print(f"\n  ← CONFIG_REQUEST — resending config", file=sys.stderr)
                    try:
                        _send_msg(sock, {
                            "type": "CONFIG",
                            "node_id": config.node_id,
                            "config_hash": _config_hash(config),
                            "config": asdict(config),
                        })
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        except socket.timeout:
            continue
        except (ConnectionResetError, OSError):
            break


# ── Streaming modes ───────────────────────────────────────────────────────────

def _stream_tcp(generator: SyntheticNodeGenerator, host: str, port: int,
                interval_ms: int = 500, cloudflare_host: Optional[str] = None):
    """Stream detection frames to the tracker server over TCP with full protocol.

    Includes chain of custody: signing, hash chain, TSA timestamping.
    """
    from chain_of_custody.crypto_backend import SoftwareCryptoBackend
    from chain_of_custody.packet_signer import PacketSigner
    from chain_of_custody.hash_chain import HashChainBuilder
    from chain_of_custody.tsa_client import TimestampManager
    from chain_of_custody.iq_buffer import IQCircularBuffer

    config = generator.config

    # Initialize chain of custody
    key_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "keys", config.node_id)
    os.makedirs(key_dir, exist_ok=True)
    crypto = SoftwareCryptoBackend(key_file=os.path.join(key_dir, "synthetic_key.json"))
    signer = PacketSigner(config.node_id, crypto)
    chain_builder = HashChainBuilder(config.node_id, crypto, asdict(config))
    tsa_manager = TimestampManager(enable_tsa=True, enable_ots=True)
    iq_buffer = IQCircularBuffer(is_synthetic=True)
    iq_buffer.start()

    print(f"  Chain of custody: key_fp={crypto.get_public_key_fingerprint()[:12]}... serial={crypto.get_serial_number()}", file=sys.stderr)

    while True:
        sock = _connect_tcp(host, port, cloudflare_host=cloudflare_host)

        # Perform handshake (with key registration)
        if not _perform_handshake(sock, config, crypto_backend=crypto):
            print("Handshake failed, reconnecting in 5s...", file=sys.stderr)
            sock.close()
            time.sleep(5)
            continue

        # Start heartbeat and listener threads
        stop_event = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop, args=(sock, config, stop_event), daemon=True
        )
        listener_thread = threading.Thread(
            target=_listener_loop, args=(sock, config, stop_event), daemon=True
        )
        hb_thread.start()
        listener_thread.start()

        try:
            last_chain_check = time.time()

            while True:
                timestamp_ms = int(time.time() * 1000)
                frame = generator.generate_frame(timestamp_ms)

                # Sign the detection frame
                signed = signer.sign_frame(frame)
                chain_builder.add_detection(signed.payload_hash)

                # Wrap in DETECTION message with signed envelope
                detection_msg = {
                    "type": "DETECTION",
                    "node_id": config.node_id,
                    "data": signed.to_dict(),
                }

                try:
                    _send_msg(sock, detection_msg)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    print("\nConnection lost, reconnecting...", file=sys.stderr)
                    break

                # Check for hour boundary — close chain and submit
                now = time.time()
                if now - last_chain_check > 60:  # Check every ~60s
                    last_chain_check = now
                    entry = chain_builder.close_hour()
                    if entry:
                        # Get TSA timestamp and OTS proof
                        tsa_token, ots_proof = tsa_manager.timestamp_entry(entry.entry_hash)
                        if tsa_token:
                            entry.tsa_token = tsa_token
                        if ots_proof:
                            entry.ots_proof = ots_proof
                        # Submit chain entry to server
                        try:
                            _send_msg(sock, {
                                "type": "CHAIN_ENTRY",
                                "node_id": config.node_id,
                                "entry": entry.to_dict(),
                            })
                            print(f"\n  → CHAIN_ENTRY (hour={entry.hour_utc}, n={entry.n_detections})", file=sys.stderr)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass

                # Print summary to stderr
                n_det = len(frame["delay"])
                n_adsb = sum(
                    1 for a in frame.get("adsb", []) if a is not None
                ) if "adsb" in frame else 0
                print(
                    f"\r[{time.strftime('%H:%M:%S')}] "
                    f"Sent signed DETECTION: {n_det} detections"
                    f"{f', {n_adsb} with ADS-B' if n_adsb else ''}"
                    f" | targets: {len(generator.targets)}"
                    f" | chain: {chain_builder.pending_detections} pending",
                    end="", file=sys.stderr,
                )

                time.sleep(interval_ms / 1000.0)

        except KeyboardInterrupt:
            print("\nStopping synthetic node.", file=sys.stderr)
            # Close any pending chain entry
            entry = chain_builder.close_hour()
            if entry:
                print(f"  Final chain entry: {entry.n_detections} detections", file=sys.stderr)
            iq_buffer.stop()
            stop_event.set()
            sock.close()
            return
        finally:
            stop_event.set()
            sock.close()

        # Brief pause before reconnection
        time.sleep(2)


def _stream_http(generator: SyntheticNodeGenerator, url: str,
                 interval_ms: int = 500, batch_size: int = 10):
    """Stream detection frames to the server over HTTP POST."""
    import urllib.request

    frames_buffer = []

    try:
        while True:
            timestamp_ms = int(time.time() * 1000)
            frame = generator.generate_frame(timestamp_ms)
            frames_buffer.append(frame)

            if len(frames_buffer) >= batch_size:
                body = json.dumps({"frames": frames_buffer}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    resp = urllib.request.urlopen(req, timeout=10)
                    result = json.loads(resp.read())
                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] "
                        f"Sent {len(frames_buffer)} frames → "
                        f"{result.get('tracks', '?')} tracks",
                        end="", file=sys.stderr,
                    )
                except Exception as exc:
                    print(f"\nHTTP POST failed: {exc}", file=sys.stderr)
                frames_buffer = []

            time.sleep(interval_ms / 1000.0)

    except KeyboardInterrupt:
        print("\nStopping synthetic node.", file=sys.stderr)


def _replay_file(filepath: str, host: str, port: int, config: NodeConfig,
                 speed: float = 1.0, cloudflare_host: Optional[str] = None):
    """Replay a .detection file over TCP with full protocol."""
    with open(filepath, "r") as f:
        content = f.read().strip()
        if not content.startswith("["):
            content = "[" + content + "]"
        frames = json.loads(content)

    if not frames:
        print("No frames in file.", file=sys.stderr)
        return

    sock = _connect_tcp(host, port, cloudflare_host=cloudflare_host)

    # Perform handshake
    if not _perform_handshake(sock, config):
        print("Handshake failed.", file=sys.stderr)
        sock.close()
        return

    # Start heartbeat thread
    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(sock, config, stop_event), daemon=True
    )
    hb_thread.start()

    try:
        prev_ts = frames[0].get("timestamp", 0)
        for i, frame in enumerate(frames):
            ts = frame.get("timestamp", 0)
            dt = (ts - prev_ts) / 1000.0 if ts > prev_ts else 0.5
            prev_ts = ts

            if dt > 0 and speed > 0:
                time.sleep(dt / speed)

            detection_msg = {
                "type": "DETECTION",
                "node_id": config.node_id,
                "data": frame,
            }

            try:
                _send_msg(sock, detection_msg)
            except (BrokenPipeError, ConnectionResetError):
                print("Connection lost, reconnecting...", file=sys.stderr)
                sock.close()
                sock = _connect_tcp(host, port, cloudflare_host=cloudflare_host)
                if not _perform_handshake(sock, config):
                    print("Handshake failed on reconnect.", file=sys.stderr)
                    break
                _send_msg(sock, detection_msg)

            n_det = len(frame.get("delay", []))
            print(
                f"\r[{i+1}/{len(frames)}] "
                f"Replayed frame: {n_det} detections",
                end="", file=sys.stderr,
            )

        print(f"\nReplayed {len(frames)} frames.", file=sys.stderr)

    except KeyboardInterrupt:
        print("\nStopping replay.", file=sys.stderr)
    finally:
        stop_event.set()
        sock.close()


def _stream_multi_node_tcp(nodes_config_path: str, host: str, port: int,
                           mode: str = "detection", interval_ms: int = 500,
                           cloudflare_host: Optional[str] = None):
    """Run multiple synthetic nodes from a shared simulation world.

    Each node gets its own TCP connection, protocol handshake, and
    chain of custody key pair (signing, hash chain).
    All nodes observe the SAME aircraft objects.

    Args:
        nodes_config_path: Path to JSON file with list of node configs.
    """
    from simulation.world import SimulationWorld, NodeConfig as WorldNodeConfig
    from chain_of_custody.crypto_backend import SoftwareCryptoBackend
    from chain_of_custody.packet_signer import PacketSigner
    from chain_of_custody.hash_chain import HashChainBuilder

    # Load node configs
    with open(nodes_config_path) as f:
        nodes_data = json.load(f)

    if isinstance(nodes_data, dict):
        nodes_data = nodes_data.get("nodes", [])

    # Build simulation world
    world = SimulationWorld()
    node_configs = []

    for nd in nodes_data:
        wc = WorldNodeConfig(
            node_id=nd.get("node_id", f"synth-node-{len(node_configs)+1:02d}"),
            rx_lat=nd.get("rx_lat", 33.939182),
            rx_lon=nd.get("rx_lon", -84.651910),
            rx_alt_ft=nd.get("rx_alt_ft", 950.0),
            tx_lat=nd.get("tx_lat", 33.75667),
            tx_lon=nd.get("tx_lon", -84.331844),
            tx_alt_ft=nd.get("tx_alt_ft", 1600.0),
            fc_hz=nd.get("fc_hz", 195_000_000.0),
            fs_hz=nd.get("fs_hz", 2_000_000.0),
            beam_width_deg=nd.get("beam_width_deg", 41.0),
            max_range_km=nd.get("max_range_km", 50.0),
        )
        world.add_node(wc)
        node_configs.append(wc)

    # Set world center to average of all node RX positions
    if node_configs:
        world.center_lat = sum(nc.rx_lat for nc in node_configs) / len(node_configs)
        world.center_lon = sum(nc.rx_lon for nc in node_configs) / len(node_configs)

    print(f"Multi-node simulation: {len(node_configs)} nodes", file=sys.stderr)
    for nc in node_configs:
        print(f"  {nc.node_id}: RX=({nc.rx_lat:.4f}, {nc.rx_lon:.4f}) beam={nc.beam_azimuth_deg:.0f}°", file=sys.stderr)

    # Connect each node with chain of custody
    node_sockets: dict[str, socket.socket] = {}
    node_local_configs: dict[str, NodeConfig] = {}
    node_crypto: dict[str, SoftwareCryptoBackend] = {}
    node_signers: dict[str, PacketSigner] = {}
    node_chains: dict[str, HashChainBuilder] = {}

    for wc in node_configs:
        local_cfg = NodeConfig(
            node_id=wc.node_id,
            rx_lat=wc.rx_lat, rx_lon=wc.rx_lon, rx_alt_ft=wc.rx_alt_ft,
            tx_lat=wc.tx_lat, tx_lon=wc.tx_lon, tx_alt_ft=wc.tx_alt_ft,
            fc_hz=wc.fc_hz, fs_hz=wc.fs_hz,
        )
        node_local_configs[wc.node_id] = local_cfg

        # Initialize chain of custody for this node
        key_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "keys", wc.node_id)
        os.makedirs(key_dir, exist_ok=True)
        crypto = SoftwareCryptoBackend(key_file=os.path.join(key_dir, "synthetic_key.json"))
        node_crypto[wc.node_id] = crypto
        node_signers[wc.node_id] = PacketSigner(wc.node_id, crypto)
        node_chains[wc.node_id] = HashChainBuilder(wc.node_id, crypto, asdict(local_cfg))

        sock = _connect_tcp(host, port, cloudflare_host=cloudflare_host)
        if not _perform_handshake(sock, local_cfg, crypto_backend=crypto):
            print(f"Handshake failed for {wc.node_id}", file=sys.stderr)
            sock.close()
            continue
        node_sockets[wc.node_id] = sock

    if not node_sockets:
        print("No nodes connected, exiting.", file=sys.stderr)
        return

    # Start heartbeat threads for all nodes
    stop_event = threading.Event()
    for nid, sock in node_sockets.items():
        t = threading.Thread(
            target=_heartbeat_loop, args=(sock, node_local_configs[nid], stop_event),
            daemon=True,
        )
        t.start()

    # Track last chain-close time per node
    last_chain_check: dict[str, float] = {nid: time.time() for nid in node_sockets}
    current_hour: dict[str, str] = {}
    for nid in node_sockets:
        current_hour[nid] = time.strftime("%Y-%m-%dT%H", time.gmtime())

    # Main simulation loop
    try:
        while True:
            dt = interval_ms / 1000.0
            world.step(dt, mode=mode)
            timestamp_ms = int(time.time() * 1000)
            all_frames = world.generate_all_frames(timestamp_ms)

            total_det = 0
            for nid, frame in all_frames.items():
                if nid not in node_sockets:
                    continue
                n_det = len(frame["delay"])
                total_det += n_det

                # Sign the detection frame
                signer = node_signers.get(nid)
                if signer:
                    signed = signer.sign_frame(frame)
                    node_chains[nid].add_detection(signed.payload_hash)
                    detection_msg = {
                        "type": "DETECTION",
                        "node_id": nid,
                        "data": frame,
                        "signature": signed.signature,
                        "payload_hash": signed.payload_hash,
                        "signing_mode": signed.signing_mode,
                        "public_key_fingerprint": signed.public_key_fingerprint,
                    }
                else:
                    detection_msg = {
                        "type": "DETECTION",
                        "node_id": nid,
                        "data": frame,
                    }

                try:
                    _send_msg(node_sockets[nid], detection_msg)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    print(f"\nLost connection for {nid}", file=sys.stderr)
                    node_sockets.pop(nid, None)

            # Check hour boundary for chain closure (every 60s)
            now = time.time()
            hour_now = time.strftime("%Y-%m-%dT%H", time.gmtime())
            for nid in list(node_sockets.keys()):
                if now - last_chain_check.get(nid, 0) < 60:
                    continue
                last_chain_check[nid] = now
                if hour_now != current_hour.get(nid):
                    chain_builder = node_chains.get(nid)
                    if chain_builder and chain_builder._detection_hashes:
                        entry = chain_builder.close_hour()
                        if entry:
                            chain_msg = {
                                "type": "CHAIN_ENTRY",
                                "node_id": nid,
                                "entry": asdict(entry),
                            }
                            try:
                                _send_msg(node_sockets[nid], chain_msg)
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                pass
                    current_hour[nid] = hour_now

            n_aircraft = len(world.aircraft)
            n_adsb = sum(1 for ac in world.aircraft if ac.has_adsb)
            n_anom = sum(1 for ac in world.aircraft if ac.is_anomalous)
            print(
                f"\r[{time.strftime('%H:%M:%S')}] "
                f"aircraft={n_aircraft} (adsb={n_adsb} anom={n_anom}) "
                f"nodes={len(node_sockets)} det_total={total_det}",
                end="", file=sys.stderr,
            )

            if not node_sockets:
                print("\nAll nodes disconnected, exiting.", file=sys.stderr)
                break

            time.sleep(dt)

    except KeyboardInterrupt:
        print("\nStopping multi-node simulation.", file=sys.stderr)
    finally:
        stop_event.set()
        # Close any pending chain entries
        for nid, chain_builder in node_chains.items():
            if chain_builder._detection_hashes:
                try:
                    chain_builder.close_hour()
                except Exception:
                    pass
        for sock in node_sockets.values():
            sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="Synthetic node for Retina passive radar network"
    )
    parser.add_argument(
        "--host", default="localhost",
        help="Tracker server host (default: localhost)",
    )
    parser.add_argument(
        "--port", type=int, default=3012,
        help="Tracker server TCP port (default: 3012)",
    )
    parser.add_argument(
        "--mode", choices=["detection", "adsb", "anomalous"],
        default="detection",
        help="Data mode: detection-only, with ADS-B, or with anomalous objects",
    )
    parser.add_argument(
        "--interval", type=int, default=500,
        help="Interval between frames in ms (default: 500)",
    )
    parser.add_argument(
        "--file",
        help="Replay a .detection file instead of generating synthetic data",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Replay speed multiplier (default: 1.0)",
    )
    parser.add_argument(
        "--http",
        help="Stream via HTTP POST to this URL instead of TCP",
    )
    parser.add_argument(
        "--cloudflare-host",
        help="Route TCP through Cloudflare frontend (e.g. hub.re) so the "
             "server sees the node as a real internet client",
    )
    parser.add_argument(
        "--config",
        help="Path to node_config.json (single node)",
    )
    parser.add_argument(
        "--nodes-config",
        help="Path to multi-node config JSON for shared-world simulation",
    )
    parser.add_argument(
        "--export-training",
        help="Export ML training data to this NDJSON file path (no server needed)",
    )
    parser.add_argument(
        "--export-frames", type=int, default=10000,
        help="Number of frames to export (default: 10000)",
    )
    parser.add_argument(
        "--node-id", default="synth-node-01",
        help="Node identifier — 'synth-' prefix marks synthetic nodes (default: synth-node-01)",
    )
    # Node geometry overrides
    parser.add_argument("--rx-lat", type=float)
    parser.add_argument("--rx-lon", type=float)
    parser.add_argument("--rx-alt-ft", type=float)
    parser.add_argument("--tx-lat", type=float)
    parser.add_argument("--tx-lon", type=float)
    parser.add_argument("--tx-alt-ft", type=float)
    parser.add_argument("--fc", type=float, help="Center frequency in Hz")

    args = parser.parse_args()

    # Build node config
    node_config = NodeConfig(node_id=args.node_id)

    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
        for k, v in cfg.items():
            if hasattr(node_config, k):
                setattr(node_config, k, v)

    # CLI overrides
    for attr in ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"):
        cli_val = getattr(args, attr.replace("-", "_"), None)
        if cli_val is not None:
            setattr(node_config, attr, cli_val)
    if args.fc is not None:
        node_config.fc_hz = args.fc

    cfg_hash = _config_hash(node_config)

    print(f"Synthetic Node: {node_config.node_id}", file=sys.stderr)
    print(f"  Mode: {args.mode}", file=sys.stderr)
    print(f"  Config hash: {cfg_hash}", file=sys.stderr)
    print(
        f"  RX: ({node_config.rx_lat:.6f}, {node_config.rx_lon:.6f}) "
        f"@ {node_config.rx_alt_ft:.0f} ft",
        file=sys.stderr,
    )
    print(
        f"  TX: ({node_config.tx_lat:.6f}, {node_config.tx_lon:.6f}) "
        f"@ {node_config.tx_alt_ft:.0f} ft",
        file=sys.stderr,
    )
    print(f"  FC: {node_config.fc_hz/1e6:.1f} MHz", file=sys.stderr)

    cf_host = args.cloudflare_host
    if cf_host:
        print(f"  Cloudflare frontend: {cf_host}", file=sys.stderr)

    # ── Multi-node shared-world mode ──────────────────────────────
    if args.nodes_config:
        print(f"  Multi-node config: {args.nodes_config}", file=sys.stderr)
        _stream_multi_node_tcp(
            args.nodes_config, args.host, args.port,
            mode=args.mode, interval_ms=args.interval,
            cloudflare_host=cf_host,
        )
        return

    # ── Single-node modes ──────────────────────────────────────────
    if args.file:
        print(f"  Replaying: {args.file} @ {args.speed}x speed", file=sys.stderr)
        _replay_file(args.file, args.host, args.port, node_config, args.speed,
                     cloudflare_host=cf_host)
    else:
        generator = SyntheticNodeGenerator(node_config, mode=args.mode)

        if args.http:
            print(f"  Streaming to: {args.http} (HTTP)", file=sys.stderr)
            _stream_http(generator, args.http, args.interval)
        else:
            print(
                f"  Streaming to: {args.host}:{args.port} (TCP)",
                file=sys.stderr,
            )
            _stream_tcp(generator, args.host, args.port, args.interval,
                        cloudflare_host=cf_host)


if __name__ == "__main__":
    main()
