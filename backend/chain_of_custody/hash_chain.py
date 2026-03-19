"""
Hourly hash chain construction.

At the top of each UTC hour, the node builds a chain entry covering all
detections logged in the past hour. Each entry includes the previous
entry's hash, making any gap or modification detectable.

Chain structure:
  Hour N-1                         Hour N
  ┌──────────────────┐          ┌──────────────────────────────┐
  │ prev_hash        │          │ prev_hash = hash(Hour N-1)   │
  │ detections[...]  │──hash──► │ detections[...this hour...]  │
  │ node config      │          │ node config                  │
  │ firmware_version │          │ firmware_version              │
  │ timestamp_utc    │          │ timestamp_utc                │
  └──────────────────┘          └──────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .crypto_backend import CryptoBackend
from .packet_signer import canonicalize
from .models import HashChainEntry

logger = logging.getLogger(__name__)

FIRMWARE_VERSION = "1.0.0-synthetic"


class HashChainBuilder:
    """Builds and maintains the hourly hash chain for a node.

    Accumulates detection hashes during the current hour window.
    When `close_hour()` is called (or automatically at hour boundary),
    produces a signed HashChainEntry and resets for the next hour.
    """

    def __init__(
        self,
        node_id: str,
        crypto: CryptoBackend,
        node_config: dict,
        chain_dir: str = "",
        firmware_version: str = FIRMWARE_VERSION,
    ):
        self.node_id = node_id
        self.crypto = crypto
        self.node_config = node_config
        self.firmware_version = firmware_version

        # Chain state
        self._prev_hash: str = "genesis"
        self._current_hour: Optional[str] = None
        self._detection_hashes: list[str] = []  # SHA-256 of each signed packet this hour
        self._n_detections: int = 0
        self._chain: list[HashChainEntry] = []

        # Persistence directory
        self._chain_dir = chain_dir or os.path.join(
            os.path.dirname(__file__), "..", "coverage_data", "chains", node_id
        )
        os.makedirs(self._chain_dir, exist_ok=True)

        # Try to load previous chain state
        self._load_chain_state()

    def _load_chain_state(self):
        """Load the last chain entry to recover prev_hash."""
        state_file = os.path.join(self._chain_dir, "chain_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                self._prev_hash = state.get("last_hash", "genesis")
                self._current_hour = state.get("current_hour")
                logger.info("Resumed chain for %s (prev_hash=%s...)", self.node_id, self._prev_hash[:12])
            except Exception as exc:
                logger.warning("Failed to load chain state for %s: %s", self.node_id, exc)

    def _save_chain_state(self):
        """Persist current chain state for recovery."""
        state_file = os.path.join(self._chain_dir, "chain_state.json")
        state = {
            "last_hash": self._prev_hash,
            "current_hour": self._current_hour,
            "n_entries": len(self._chain),
        }
        with open(state_file, "w") as f:
            json.dump(state, f)

    @staticmethod
    def _hour_key(ts: Optional[float] = None) -> str:
        """Get the current UTC hour as ISO string."""
        dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:00:00Z")

    def add_detection(self, payload_hash: str):
        """Record a detection's hash for the current hour's chain entry.

        Called after each detection frame is signed. The payload_hash is
        the SHA-256 of the canonical JSON payload from PacketSigner.
        """
        current = self._hour_key()

        # Check if we've crossed an hour boundary
        if self._current_hour and current != self._current_hour and self._detection_hashes:
            self.close_hour()

        self._current_hour = current
        self._detection_hashes.append(payload_hash)
        self._n_detections += 1

    def close_hour(self) -> Optional[HashChainEntry]:
        """Close the current hour and produce a signed HashChainEntry.

        Returns the entry, or None if there were no detections.
        """
        if not self._detection_hashes:
            return None

        hour_utc = self._current_hour or self._hour_key()

        # Hash all detections together
        detections_blob = canonicalize({"hashes": self._detection_hashes})
        detections_hash = self.crypto.hash_sha256(detections_blob)

        # Node config hash
        config_canonical = canonicalize(self.node_config)
        node_config_hash = self.crypto.hash_sha256(config_canonical)

        now_utc = datetime.now(timezone.utc).isoformat()

        # Build the entry (without signature fields)
        entry_data = {
            "node_id": self.node_id,
            "hour_utc": hour_utc,
            "prev_hash": self._prev_hash,
            "detections_hash": detections_hash,
            "n_detections": self._n_detections,
            "node_config_hash": node_config_hash,
            "firmware_version": self.firmware_version,
            "timestamp_utc": now_utc,
        }

        # Hash and sign the entry
        entry_canonical = canonicalize(entry_data)
        entry_hash = self.crypto.hash_sha256(entry_canonical)
        signature = self.crypto.sign_hex(entry_canonical)

        entry = HashChainEntry(
            node_id=self.node_id,
            hour_utc=hour_utc,
            prev_hash=self._prev_hash,
            detections_hash=detections_hash,
            n_detections=self._n_detections,
            node_config_hash=node_config_hash,
            firmware_version=self.firmware_version,
            timestamp_utc=now_utc,
            entry_hash=entry_hash,
            signature=signature,
            signing_mode=self.crypto.signing_mode,
        )

        # Update chain state
        self._prev_hash = entry_hash
        self._chain.append(entry)
        self._detection_hashes = []
        self._n_detections = 0
        self._current_hour = None

        # Persist entry to disk (append-only)
        self._persist_entry(entry)
        self._save_chain_state()

        logger.info(
            "Chain entry for %s hour=%s: %d detections, hash=%s...",
            self.node_id, hour_utc, entry.n_detections, entry_hash[:12],
        )
        return entry

    def _persist_entry(self, entry: HashChainEntry):
        """Write a chain entry to the append-only chain log."""
        log_file = os.path.join(self._chain_dir, "chain_log.jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def get_chain(self) -> list[HashChainEntry]:
        """Return all chain entries built during this session."""
        return list(self._chain)

    def get_latest_entry(self) -> Optional[HashChainEntry]:
        """Return the most recent chain entry."""
        return self._chain[-1] if self._chain else None

    @property
    def pending_detections(self) -> int:
        """Number of detections accumulated in the current (unclosed) hour."""
        return len(self._detection_hashes)

    @property
    def prev_hash(self) -> str:
        return self._prev_hash


class HashChainVerifier:
    """Server-side verification of hash chain entries.

    Verifies:
    1. Chain linkage (prev_hash matches previous entry)
    2. Entry hash correctness
    3. Signature validity
    """

    def __init__(self, get_public_key: callable):
        self._get_key = get_public_key

    def verify_entry(self, entry: HashChainEntry, expected_prev_hash: str = "") -> tuple[bool, str]:
        """Verify a single chain entry.

        Returns (valid, reason).
        """
        # 1. Check prev_hash linkage
        if expected_prev_hash and entry.prev_hash != expected_prev_hash:
            return False, f"prev_hash mismatch: expected {expected_prev_hash[:12]}..., got {entry.prev_hash[:12]}..."

        # 2. Reconstruct and verify entry_hash
        entry_data = {
            "node_id": entry.node_id,
            "hour_utc": entry.hour_utc,
            "prev_hash": entry.prev_hash,
            "detections_hash": entry.detections_hash,
            "n_detections": entry.n_detections,
            "node_config_hash": entry.node_config_hash,
            "firmware_version": entry.firmware_version,
            "timestamp_utc": entry.timestamp_utc,
        }
        entry_canonical = canonicalize(entry_data)
        import hashlib
        computed_hash = hashlib.sha256(entry_canonical).hexdigest()

        if computed_hash != entry.entry_hash:
            return False, f"entry_hash mismatch: computed {computed_hash[:12]}..., got {entry.entry_hash[:12]}..."

        # 3. Verify signature
        pem = self._get_key(entry.node_id)
        if not pem:
            return False, f"no public key for node {entry.node_id}"

        try:
            sig_bytes = bytes.fromhex(entry.signature)
            from cryptography.hazmat.primitives import serialization as ser
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import hashes
            from cryptography.exceptions import InvalidSignature

            pub_key = ser.load_pem_public_key(pem.encode())
            pub_key.verify(sig_bytes, entry_canonical, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return False, "invalid signature"
        except Exception as exc:
            return False, f"verification error: {exc}"

        return True, "ok"

    def verify_chain(self, entries: list[HashChainEntry]) -> tuple[bool, list[str]]:
        """Verify a sequence of chain entries.

        Returns (all_valid, list_of_issues).
        """
        issues = []
        expected_prev = ""

        for i, entry in enumerate(entries):
            if i == 0 and entry.prev_hash == "genesis":
                expected_prev = ""  # Genesis entry has no predecessor

            valid, reason = self.verify_entry(entry, expected_prev)
            if not valid:
                issues.append(f"Entry {i} ({entry.hour_utc}): {reason}")

            expected_prev = entry.entry_hash

        return len(issues) == 0, issues
