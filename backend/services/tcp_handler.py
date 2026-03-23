"""TCP server handler implementing the RETINA node protocol.

Handles: HELLO → CONFIG → HEARTBEAT → DETECTION → chain-of-custody messages.
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core import state
from chain_of_custody.hash_chain import HashChainVerifier, HashChainEntry

# Dedicated single-thread executor for node registration.
# Registration is serialized by an internal lock anyway (O(n²) overlap zones),
# so one thread is sufficient. Keeping it separate prevents registration from
# starving the default executor used by frame processor workers.
_registration_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-reg")

# Lazy import to avoid circular dependency — routes.admin must be importable first
def _log_event(category: str, message: str, severity: str = "info", meta: dict | None = None):
    try:
        from routes.admin import log_event
        log_event(category, message, severity, meta)
    except Exception:
        pass

RETINA_PROTOCOL_VERSION = "1.0"
SERVER_CAPABILITIES = {
    "config_request": True,
    "adsb_report": True,
    "association": True,
    "analytics": True,
    "coverage_map": True,
}


def is_synthetic_node(node_id: str) -> bool:
    """Detect synthetic nodes by their 'synth-' ID prefix."""
    return node_id.startswith("synth-")


async def _send_msg(writer: asyncio.StreamWriter, msg: dict):
    writer.write(json.dumps(msg).encode("utf-8") + b"\n")
    await writer.drain()


async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single TCP connection from a radar node.

    Implements the RETINA node protocol:
      1. Node sends HELLO → server validates version
      2. Node sends CONFIG → server stores config, replies CONFIG_ACK
      3. Steady state: node sends HEARTBEAT + DETECTION messages
      4. Server sends CONFIG_REQUEST if heartbeat config hash mismatches
    """
    peer = writer.get_extra_info("peername")
    logging.info("Radar TCP: new connection from %s", peer)
    buf = b""
    node_id = None

    try:
        while True:
            chunk = await reader.read(4096)
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
                    logging.debug("Radar TCP: malformed JSON from %s", peer)
                    continue

                msg_type = msg.get("type")

                # ── HELLO ──────────────────────────────────────────
                if msg_type == "HELLO":
                    node_id = msg.get("node_id", f"unknown-{peer}")
                    version = msg.get("version", "0.0")
                    is_synth = msg.get("is_synthetic", is_synthetic_node(node_id))
                    caps = msg.get("capabilities", {})
                    logging.info("Radar TCP: HELLO from %s (v%s, synthetic=%s, caps=%s)",
                                 node_id, version, is_synth, list(caps.keys()))
                    continue

                # ── CONFIG ─────────────────────────────────────────
                if msg_type == "CONFIG":
                    if node_id is None:
                        node_id = msg.get("node_id", f"unknown-{peer}")
                    config_hash = msg.get("config_hash", "")
                    config_payload = msg.get("config", {})
                    is_synth = msg.get("is_synthetic", is_synthetic_node(node_id))
                    state.connected_nodes[node_id] = {
                        "config_hash": config_hash,
                        "config": config_payload,
                        "status": "active",
                        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                        "peer": str(peer),
                        "is_synthetic": is_synth,
                        "capabilities": msg.get("capabilities", {}),
                    }
                    logging.info("Radar TCP: CONFIG from %s (hash=%s, synthetic=%s)",
                                 node_id, config_hash, is_synth)
                    _log_event(
                        "node",
                        f"Node {node_id} connected (hash={config_hash[:8]}, synthetic={is_synth})",
                        "info",
                        {"node_id": node_id, "config_hash": config_hash, "is_synthetic": is_synth},
                    )
                    await _send_msg(writer, {
                        "type": "CONFIG_ACK",
                        "config_hash": config_hash,
                        "server_version": RETINA_PROTOCOL_VERSION,
                        "server_capabilities": SERVER_CAPABILITIES,
                    })
                    # Run registration in the dedicated single-thread executor so
                    # it never starves the default executor used by frame workers.
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        _registration_executor,
                        lambda: (
                            state.node_analytics.register_node(node_id, config_payload),
                            state.node_associator.register_node(node_id, config_payload),
                        ),
                    )
                    continue

                # ── REGISTER_KEY (chain of custody) ────────────────
                if msg_type == "REGISTER_KEY":
                    _handle_register_key(msg, node_id, writer)
                    continue

                # ── CHAIN_ENTRY ────────────────────────────────────
                if msg_type == "CHAIN_ENTRY":
                    await _handle_chain_entry(msg, node_id, writer)
                    continue

                # ── IQ_COMMITMENT ──────────────────────────────────
                if msg_type == "IQ_COMMITMENT":
                    await _handle_iq_commitment(msg, node_id, writer)
                    continue

                # ── HEARTBEAT ──────────────────────────────────────
                if msg_type == "HEARTBEAT":
                    await _handle_heartbeat(msg, node_id, writer)
                    continue

                # ── DETECTION ──────────────────────────────────────
                if msg_type == "DETECTION":
                    _enqueue_detection(msg, node_id)
                    # Yield to the event loop after each detection so
                    # HTTP handlers and other coroutines can run.
                    await asyncio.sleep(0)
                    continue

                # ── Legacy bare detection frame ────────────────────
                if "timestamp" in msg and msg_type is None:
                    if node_id:
                        msg["_node_id"] = node_id
                    try:
                        state.frame_queue.put_nowait((node_id or "tcp-unknown", msg))
                    except asyncio.QueueFull:
                        pass
                    continue

    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        if node_id and node_id in state.connected_nodes:
            state.connected_nodes[node_id]["status"] = "disconnected"
            _log_event(
                "node",
                f"Node {node_id} disconnected",
                "warning",
                {"node_id": node_id},
            )
        logging.info("Radar TCP: connection closed from %s (node=%s)", peer, node_id)
        writer.close()


# ── Sub-handlers (keep handle_tcp_client readable) ────────────────────────────

async def _handle_register_key(msg: dict, node_id: str | None, writer):
    from chain_of_custody.models import NodeIdentity

    key_node_id = msg.get("node_id", node_id)
    pub_key_pem = msg.get("public_key_pem", "")
    fingerprint = msg.get("fingerprint", "")
    serial = msg.get("serial_number", "")
    signing_mode = msg.get("signing_mode", "software")
    if pub_key_pem and key_node_id:
        identity = NodeIdentity(
            node_id=key_node_id,
            public_key_pem=pub_key_pem,
            public_key_fingerprint=fingerprint,
            serial_number=serial,
            signing_mode=signing_mode,
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        state.node_identities[key_node_id] = identity
        state.sig_verifier.register_key(key_node_id, pub_key_pem)
        logging.info("Chain of custody: registered key for %s (mode=%s, fp=%s)",
                     key_node_id, signing_mode, fingerprint[:12])
        await _send_msg(writer, {
            "type": "KEY_ACK",
            "node_id": key_node_id,
            "fingerprint": fingerprint,
            "status": "registered",
        })


async def _handle_chain_entry(msg: dict, node_id: str | None, writer):
    entry_data = msg.get("entry", {})
    ce_node_id = entry_data.get("node_id", node_id)
    if not ce_node_id:
        return
    if ce_node_id not in state.chain_entries:
        state.chain_entries[ce_node_id] = []
    verified = False
    if ce_node_id in state.node_identities:
        try:
            entry_obj = HashChainEntry.from_dict(entry_data)
            verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
            valid, reason = verifier.verify_entry(entry_obj)
            verified = valid
            if not valid:
                logging.warning("Chain entry verification failed for %s: %s", ce_node_id, reason)
        except Exception as exc:
            logging.warning("Chain entry parse error for %s: %s", ce_node_id, exc)
    entry_data["_verified"] = verified
    entry_data["_received_at"] = datetime.now(timezone.utc).isoformat()
    state.chain_entries[ce_node_id].append(entry_data)
    logging.info("Chain entry from %s (hour=%s, verified=%s)",
                 ce_node_id, entry_data.get("hour_utc"), verified)
    await _send_msg(writer, {
        "type": "CHAIN_ENTRY_ACK",
        "node_id": ce_node_id,
        "entry_hash": entry_data.get("entry_hash", ""),
        "verified": verified,
    })


async def _handle_iq_commitment(msg: dict, node_id: str | None, writer):
    iq_data = msg.get("capture", {})
    iq_node_id = iq_data.get("node_id", node_id)
    if not iq_node_id:
        return
    if iq_node_id not in state.iq_commitments:
        state.iq_commitments[iq_node_id] = []
    iq_data["_received_at"] = datetime.now(timezone.utc).isoformat()
    state.iq_commitments[iq_node_id].append(iq_data)
    logging.info("IQ commitment from %s (capture=%s, hash=%s...)",
                 iq_node_id, iq_data.get("capture_id"), iq_data.get("iq_hash", "")[:12])
    await _send_msg(writer, {
        "type": "IQ_COMMITMENT_ACK",
        "node_id": iq_node_id,
        "capture_id": iq_data.get("capture_id", ""),
        "status": "committed",
    })


async def _handle_heartbeat(msg: dict, node_id: str | None, writer):
    hb_node_id = msg.get("node_id", node_id)
    hb_hash = msg.get("config_hash", "")
    hb_status = msg.get("status", "active")
    if hb_node_id and hb_node_id in state.connected_nodes:
        state.connected_nodes[hb_node_id]["last_heartbeat"] = (
            msg.get("timestamp") or datetime.now(timezone.utc).isoformat()
        )
        state.connected_nodes[hb_node_id]["status"] = hb_status
        state.node_analytics.record_heartbeat(hb_node_id)
        stored_hash = state.connected_nodes[hb_node_id].get("config_hash", "")
        if stored_hash and hb_hash != stored_hash:
            logging.warning("Radar TCP: config drift for %s (expected=%s got=%s)",
                            hb_node_id, stored_hash, hb_hash)
            _log_event(
                "config",
                f"Config drift detected for {hb_node_id} (expected={stored_hash[:8]}, got={hb_hash[:8]})",
                "warning",
                {"node_id": hb_node_id, "expected_hash": stored_hash, "actual_hash": hb_hash},
            )
            await _send_msg(writer, {"type": "CONFIG_REQUEST", "node_id": hb_node_id})


def _enqueue_detection(msg: dict, node_id: str | None):
    frame = msg.get("data", msg)
    # Signature verification is CPU-intensive — defer to the frame
    # processor thread pool instead of blocking the event loop.
    if "signature" in frame and "payload_hash" in frame:
        frame["_needs_sig_verify"] = True
    if "timestamp" not in frame and "timestamp_ms" in frame:
        frame["timestamp"] = frame["timestamp_ms"]
    if "timestamp" not in frame:
        return
    if node_id:
        frame["_node_id"] = node_id
    try:
        state.frame_queue.put_nowait((node_id or "tcp-unknown", frame))
    except asyncio.QueueFull:
        state.frames_dropped += 1
        logging.warning("Frame queue full, dropping TCP frame from %s", node_id)


def _apply_synthetic_adsb(msg: dict, node_id: str):
    """Fast-path for synthetic nodes: store ADS-B positions directly in state."""
    import time as _time
    frame = msg.get("data", msg)
    adsb_list = frame.get("adsb")
    if not adsb_list:
        return
    ts_ms = int(_time.time() * 1000)
    for entry in adsb_list:
        if not isinstance(entry, dict):
            continue
        hex_code = entry.get("hex")
        if not hex_code:
            continue
        state.adsb_aircraft[hex_code] = {
            "hex": hex_code,
            "flight": entry.get("flight", ""),
            "lat": entry.get("lat", 0),
            "lon": entry.get("lon", 0),
            "alt_baro": entry.get("alt_baro", 0),
            "gs": entry.get("gs", 0),
            "track": entry.get("track", 0),
            "last_seen_ms": ts_ms,
        }
    state.aircraft_dirty = True
