"""
IQ circular buffer and server-triggered capture.

Each node maintains a 2-minute circular buffer of raw IQ samples in RAM.
No IQ data is written to disk during normal operation.

When the server requests a capture, the node:
1. Freezes the buffer
2. Hashes the IQ data
3. Signs the hash
4. Requests a TSA timestamp
5. Sends hash + TSA token to server (commitment)
6. THEN uploads IQ data

For synthetic nodes, placeholder IQ data is generated since there's no
real SDR hardware.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import time
import threading
import uuid
from collections import deque
from typing import Optional

from .crypto_backend import CryptoBackend
from .models import IQCapturePackage
from .tsa_client import TSAClient

logger = logging.getLogger(__name__)

# 2 minutes of IQ samples at 2 MHz sample rate × 2 channels (I+Q) × 2 bytes per sample
# = 2 × 60 × 2e6 × 4 = 960 MB for real data. For synthetic, we use small chunks.
DEFAULT_BUFFER_DURATION_S = 120
SYNTHETIC_CHUNK_INTERVAL_S = 1.0  # Generate a synthetic chunk every 1s
SYNTHETIC_CHUNK_SIZE = 1024       # Small placeholder chunks for testing


class IQCircularBuffer:
    """Circular buffer for raw IQ samples in RAM.

    For real nodes: stores raw SDR IQ samples.
    For synthetic nodes: stores placeholder data for protocol testing.
    """

    def __init__(self, duration_s: float = DEFAULT_BUFFER_DURATION_S, is_synthetic: bool = True):
        self.duration_s = duration_s
        self.is_synthetic = is_synthetic
        self._lock = threading.Lock()
        # Deque of (timestamp_ms, chunk_bytes) tuples
        self._buffer: deque[tuple[int, bytes]] = deque()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the buffer (synthetic: generates placeholder data periodically)."""
        if self.is_synthetic:
            self._running = True
            self._thread = threading.Thread(target=self._synthetic_fill_loop, daemon=True)
            self._thread.start()
            logger.info("IQ circular buffer started (synthetic, duration=%ds)", self.duration_s)

    def stop(self):
        """Stop the buffer."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _synthetic_fill_loop(self):
        """Generate synthetic placeholder IQ chunks."""
        while self._running:
            ts_ms = int(time.time() * 1000)
            # Placeholder: random-ish bytes (not real IQ, just for protocol testing)
            chunk = os.urandom(SYNTHETIC_CHUNK_SIZE)
            self.append(ts_ms, chunk)
            time.sleep(SYNTHETIC_CHUNK_INTERVAL_S)

    def append(self, timestamp_ms: int, chunk: bytes):
        """Add an IQ chunk to the buffer."""
        now = time.time()
        cutoff_ms = int((now - self.duration_s) * 1000)

        with self._lock:
            self._buffer.append((timestamp_ms, chunk))
            # Evict old chunks
            while self._buffer and self._buffer[0][0] < cutoff_ms:
                self._buffer.popleft()

    def freeze(self, window_start_ms: int, window_end_ms: int) -> bytes:
        """Extract and concatenate IQ data for a time window.

        Returns the raw bytes for the requested window.
        """
        with self._lock:
            chunks = []
            for ts_ms, chunk in self._buffer:
                if window_start_ms <= ts_ms <= window_end_ms:
                    chunks.append(chunk)
            return b"".join(chunks)


class IQCaptureManager:
    """Manages IQ capture requests from the server.

    Implements the critical ordering: hash → sign → TSA → commitment → upload.
    """

    def __init__(
        self,
        node_id: str,
        crypto: CryptoBackend,
        iq_buffer: IQCircularBuffer,
        node_config: dict,
        enable_tsa: bool = True,
    ):
        self.node_id = node_id
        self.crypto = crypto
        self.iq_buffer = iq_buffer
        self.node_config = node_config
        self.tsa = TSAClient() if enable_tsa else None
        self._captures: list[IQCapturePackage] = []

    def capture(
        self,
        window_start_ms: int,
        window_end_ms: int,
        trigger_reason: str = "",
    ) -> tuple[IQCapturePackage, bytes]:
        """Execute a capture: freeze → hash → sign → TSA.

        Returns (metadata_package, iq_bytes).

        The caller should:
        1. Send the metadata (hash + signature + TSA token) to the server
        2. Wait for server acknowledgment
        3. THEN upload the IQ data

        Critical: commitment MUST precede data upload.
        """
        # 1. Freeze buffer
        iq_data = self.iq_buffer.freeze(window_start_ms, window_end_ms)
        if not iq_data:
            logger.warning("No IQ data in window %d-%d", window_start_ms, window_end_ms)

        # 2. Hash
        iq_hash = hashlib.sha256(iq_data).hexdigest()

        # 3. Sign
        signature = self.crypto.sign_hex(iq_data)

        # 4. TSA timestamp
        tsa_token = None
        if self.tsa:
            tsa_token = self.tsa.request_timestamp(iq_hash)

        # 5. Config hash
        import json
        from .packet_signer import canonicalize
        config_hash = self.crypto.hash_sha256(canonicalize(self.node_config))

        capture_id = f"iq-{uuid.uuid4().hex[:12]}"

        package = IQCapturePackage(
            node_id=self.node_id,
            capture_id=capture_id,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            iq_hash=iq_hash,
            signature=signature,
            signing_mode=self.crypto.signing_mode,
            tsa_token=tsa_token,
            node_config_hash=config_hash,
            trigger_reason=trigger_reason,
            iq_size_bytes=len(iq_data),
        )

        self._captures.append(package)
        logger.info(
            "IQ capture %s: %d bytes, hash=%s..., tsa=%s",
            capture_id, len(iq_data), iq_hash[:12],
            "ok" if tsa_token else "none",
        )

        return package, iq_data

    @property
    def captures(self) -> list[IQCapturePackage]:
        return list(self._captures)
