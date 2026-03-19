"""
RETINA Chain of Custody — Cryptographic integrity for detection data.

Provides:
- CryptoBackend abstraction (hardware ATECC608B / software P-256 stub)
- Detection packet signing & verification
- Hourly hash chain construction
- TSA / OpenTimestamps timestamping
- IQ circular buffer with server-triggered capture
"""

from .crypto_backend import SoftwareCryptoBackend, CryptoBackend
from .packet_signer import PacketSigner
from .hash_chain import HashChainBuilder
from .models import SignedPacket, HashChainEntry

__all__ = [
    "CryptoBackend",
    "SoftwareCryptoBackend",
    "PacketSigner",
    "HashChainBuilder",
    "SignedPacket",
    "HashChainEntry",
]
