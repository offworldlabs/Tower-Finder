"""
Crypto backend abstraction — hardware (ATECC608B) and software (P-256 ECDSA) modes.

The abstract CryptoBackend defines the interface; concrete implementations:
- SoftwareCryptoBackend: Uses Python `cryptography` library for P-256 ECDSA.
  Key pair generated on first run, persisted to `synthetic_key.json`.
- HardwareCryptoBackend: Placeholder for ATECC608B secure element via I2C.
  Will be implemented when Raspberry Pi nodes are deployed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)


class CryptoBackend(ABC):
    """Abstract interface for cryptographic operations."""

    @abstractmethod
    def get_public_key_pem(self) -> str:
        """Return the PEM-encoded public key."""

    @abstractmethod
    def get_public_key_fingerprint(self) -> str:
        """Return first 16 hex chars of SHA-256(DER-encoded public key)."""

    @abstractmethod
    def get_serial_number(self) -> str:
        """Return the hardware serial (ATECC608B) or generated UUID."""

    @abstractmethod
    def sign(self, data: bytes) -> bytes:
        """Sign data with the private key. Returns raw signature bytes."""

    @abstractmethod
    def verify(self, data: bytes, signature: bytes, public_key_pem: str) -> bool:
        """Verify a signature against a public key. Returns True if valid."""

    @property
    @abstractmethod
    def signing_mode(self) -> str:
        """Return 'hardware' or 'software'."""

    def hash_sha256(self, data: bytes) -> str:
        """Compute SHA-256 hex digest."""
        return hashlib.sha256(data).hexdigest()

    def sign_hex(self, data: bytes) -> str:
        """Sign data and return hex-encoded signature."""
        return self.sign(data).hex()


class SoftwareCryptoBackend(CryptoBackend):
    """Software P-256 ECDSA backend for synthetic nodes.

    Generates a key pair on first instantiation and persists it to
    `key_file` in the working directory. Each synthetic node instance
    should have its own working directory / key file.
    """

    def __init__(self, key_file: str = "synthetic_key.json"):
        self._key_file = key_file
        self._private_key: ec.EllipticCurvePrivateKey
        self._public_key: ec.EllipticCurvePublicKey
        self._serial: str
        self._load_or_generate()

    def _load_or_generate(self):
        """Load existing key pair or generate a new one."""
        if os.path.exists(self._key_file):
            try:
                with open(self._key_file, "r") as f:
                    data = json.load(f)
                self._private_key = serialization.load_pem_private_key(
                    data["private_key_pem"].encode(), password=None
                )
                self._public_key = self._private_key.public_key()
                self._serial = data.get("serial", str(uuid.uuid4()))
                logger.info("Loaded existing key pair from %s", self._key_file)
                return
            except Exception as exc:
                logger.warning("Failed to load key from %s: %s — regenerating", self._key_file, exc)

        # Generate new P-256 key pair
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        self._public_key = self._private_key.public_key()
        self._serial = f"SYN-{uuid.uuid4().hex[:12].upper()}"

        # Persist
        private_pem = self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        key_data = {
            "private_key_pem": private_pem,
            "public_key_pem": self.get_public_key_pem(),
            "serial": self._serial,
            "fingerprint": self.get_public_key_fingerprint(),
        }
        os.makedirs(os.path.dirname(self._key_file) or ".", exist_ok=True)
        with open(self._key_file, "w") as f:
            json.dump(key_data, f, indent=2)
        os.chmod(self._key_file, 0o600)
        logger.info("Generated new P-256 key pair → %s (serial=%s)", self._key_file, self._serial)

    def get_public_key_pem(self) -> str:
        return self._public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def get_public_key_fingerprint(self) -> str:
        der = self._public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(der).hexdigest()[:16]

    def get_serial_number(self) -> str:
        return self._serial

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data, ec.ECDSA(hashes.SHA256()))

    def verify(self, data: bytes, signature: bytes, public_key_pem: str) -> bool:
        try:
            pub_key = serialization.load_pem_public_key(public_key_pem.encode())
            pub_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
        except Exception as exc:
            logger.warning("Signature verification error: %s", exc)
            return False

    @property
    def signing_mode(self) -> str:
        return "software"


class HardwareCryptoBackend(CryptoBackend):
    """ATECC608B secure element backend (placeholder for Raspberry Pi deployment).

    On real hardware, this will:
    - Communicate via I2C at address 0x6A
    - Call atcab_genkey(0) on first boot (key never leaves the chip)
    - Sign using on-chip ECDSA P-256
    - Read public key and serial from the chip

    For now, raises NotImplementedError — will be implemented when
    hardware nodes are deployed.
    """

    def __init__(self):
        raise NotImplementedError(
            "HardwareCryptoBackend requires ATECC608B hardware. "
            "Use SoftwareCryptoBackend for synthetic/test nodes."
        )

    def get_public_key_pem(self) -> str:
        raise NotImplementedError

    def get_public_key_fingerprint(self) -> str:
        raise NotImplementedError

    def get_serial_number(self) -> str:
        raise NotImplementedError

    def sign(self, data: bytes) -> bytes:
        raise NotImplementedError

    def verify(self, data: bytes, signature: bytes, public_key_pem: str) -> bool:
        raise NotImplementedError

    @property
    def signing_mode(self) -> str:
        return "hardware"


# ── Server-side verification helper ──────────────────────────────────────────

class SignatureVerifier:
    """Server-side signature verification using registered public keys.

    Maintains a registry of node_id → public_key_pem and verifies
    incoming signed packets.
    """

    def __init__(self):
        self._keys: dict[str, str] = {}  # node_id → public_key_pem
        self._backend = _VerifierBackend()

    def register_key(self, node_id: str, public_key_pem: str):
        """Register a node's public key for future verification."""
        self._keys[node_id] = public_key_pem
        logger.info("Registered public key for node %s", node_id)

    def get_key(self, node_id: str) -> str | None:
        return self._keys.get(node_id)

    def verify_packet(self, node_id: str, payload_hash: str, signature_hex: str) -> bool:
        """Verify a signed packet from a registered node.

        Returns True if the signature is valid, False otherwise.
        """
        pem = self._keys.get(node_id)
        if not pem:
            logger.warning("No registered public key for node %s", node_id)
            return False

        try:
            sig_bytes = bytes.fromhex(signature_hex)
            return self._backend.verify(
                payload_hash.encode("utf-8"),
                sig_bytes,
                pem,
            )
        except (ValueError, Exception) as exc:
            logger.warning("Verification failed for node %s: %s", node_id, exc)
            return False

    @property
    def registered_nodes(self) -> list[str]:
        return list(self._keys.keys())


class _VerifierBackend:
    """Lightweight verification-only backend (no private key needed)."""

    def verify(self, data: bytes, signature: bytes, public_key_pem: str) -> bool:
        try:
            pub_key = serialization.load_pem_public_key(public_key_pem.encode())
            pub_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
        except Exception as exc:
            logger.warning("Verification error: %s", exc)
            return False
