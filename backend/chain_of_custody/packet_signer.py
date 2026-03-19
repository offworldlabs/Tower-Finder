"""
Detection packet signing — canonical JSON serialization and ECDSA signing.

Each detection frame is serialized to canonical JSON (sorted keys, no whitespace),
hashed with SHA-256, and signed with the node's private key. The signature and
metadata are attached to produce a SignedPacket.
"""

from __future__ import annotations

import json
import logging
import time

from .crypto_backend import CryptoBackend
from .models import SignedPacket

logger = logging.getLogger(__name__)


def canonicalize(data: dict) -> bytes:
    """Produce canonical JSON: sorted keys, no whitespace, ensure_ascii.

    This is the standard representation used for hashing and signing.
    All parties (node and server) must use this exact serialization.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class PacketSigner:
    """Signs detection frames to produce SignedPackets.

    Used by nodes to cryptographically bind detection data to their identity.
    """

    def __init__(self, node_id: str, crypto: CryptoBackend):
        self.node_id = node_id
        self.crypto = crypto
        self._fingerprint = crypto.get_public_key_fingerprint()

    def sign_frame(self, frame: dict) -> SignedPacket:
        """Sign a detection frame and return a SignedPacket.

        The frame dict should contain: timestamp, delay[], doppler[], snr[], adsb[]?
        """
        # Build the signable payload (only detection data, no internal fields)
        payload = {
            "node_id": self.node_id,
            "timestamp": frame.get("timestamp", int(time.time() * 1000)),
            "delay": frame.get("delay", []),
            "doppler": frame.get("doppler", []),
            "snr": frame.get("snr", []),
        }
        if "adsb" in frame:
            payload["adsb"] = frame["adsb"]

        canonical = canonicalize(payload)
        payload_hash = self.crypto.hash_sha256(canonical)
        # Sign the hash string (so server can verify with just hash + signature)
        signature = self.crypto.sign_hex(payload_hash.encode("utf-8"))

        return SignedPacket(
            node_id=self.node_id,
            timestamp_ms=payload["timestamp"],
            payload_hash=payload_hash,
            signature=signature,
            signing_mode=self.crypto.signing_mode,
            public_key_fingerprint=self._fingerprint,
            delay=payload["delay"],
            doppler=payload["doppler"],
            snr=payload["snr"],
            adsb=payload.get("adsb"),
        )

    def sign_data(self, data: bytes) -> tuple[str, str]:
        """Sign arbitrary data. Returns (hash_hex, signature_hex)."""
        h = self.crypto.hash_sha256(data)
        sig = self.crypto.sign_hex(data)
        return h, sig


class PacketVerifier:
    """Server-side verification of SignedPackets.

    Reconstructs the canonical payload from the SignedPacket fields
    and verifies the signature against the registered public key.
    """

    def __init__(self, get_public_key: callable):
        """
        Args:
            get_public_key: callable(node_id) -> str|None returning PEM public key
        """
        self._get_key = get_public_key

    def verify(self, packet: SignedPacket) -> bool:
        """Verify a signed packet's integrity and authenticity.

        Returns True if signature is valid.
        """
        pem = self._get_key(packet.node_id)
        if not pem:
            logger.warning("No public key registered for node %s", packet.node_id)
            return False

        # Reconstruct canonical payload and recompute hash
        payload = {
            "node_id": packet.node_id,
            "timestamp": packet.timestamp_ms,
            "delay": packet.delay,
            "doppler": packet.doppler,
            "snr": packet.snr,
        }
        if packet.adsb is not None:
            payload["adsb"] = packet.adsb

        canonical = canonicalize(payload)
        import hashlib as _hl
        computed_hash = _hl.sha256(canonical).hexdigest()

        if computed_hash != packet.payload_hash:
            logger.warning("Payload hash mismatch for node %s", packet.node_id)
            return False

        try:
            sig_bytes = bytes.fromhex(packet.signature)
            from cryptography.hazmat.primitives import serialization as ser
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import hashes
            from cryptography.exceptions import InvalidSignature

            pub_key = ser.load_pem_public_key(pem.encode())
            # Signature is over the hash string bytes
            pub_key.verify(sig_bytes, packet.payload_hash.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            logger.warning("Invalid signature from node %s", packet.node_id)
            return False
        except Exception as exc:
            logger.warning("Verification error for node %s: %s", packet.node_id, exc)
            return False
