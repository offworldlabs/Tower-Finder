"""
TSA (Timestamping Authority) and OpenTimestamps integration.

Node-direct timestamping — removes Offworld Labs from the trust path.
Each node makes its own requests: one per hour per node.

Two independent timestamping systems:
1. DigiCert TSA: RFC 3161 timestamps (fast, legally recognized)
2. OpenTimestamps: Bitcoin-anchored immutability (long-term, decentralized)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)

# DigiCert TSA endpoint (free, no API key required)
DIGICERT_TSA_URL = "http://timestamp.digicert.com"

# OpenTimestamps public calendar servers
OTS_CALENDAR_URLS = [
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
]


def _build_tsa_request(digest: bytes) -> bytes:
    """Build a minimal RFC 3161 TimeStampReq for SHA-256 digest.

    ASN.1 DER encoding of:
    TimeStampReq ::= SEQUENCE {
        version         INTEGER { v1(1) },
        messageImprint  MessageImprint,
        nonce           INTEGER OPTIONAL,
        certReq         BOOLEAN DEFAULT FALSE
    }
    MessageImprint ::= SEQUENCE {
        hashAlgorithm   AlgorithmIdentifier,
        hashedMessage    OCTET STRING
    }
    """
    # SHA-256 OID: 2.16.840.1.101.3.4.2.1
    sha256_oid = b"\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x01"
    # AlgorithmIdentifier: SEQUENCE { oid, NULL }
    alg_id = b"\x30" + bytes([len(sha256_oid) + 2]) + sha256_oid + b"\x05\x00"
    # hashedMessage: OCTET STRING
    hashed_msg = b"\x04" + bytes([len(digest)]) + digest
    # MessageImprint: SEQUENCE { algId, hashedMessage }
    msg_imprint = b"\x30" + bytes([len(alg_id) + len(hashed_msg)]) + alg_id + hashed_msg
    # nonce (8 random bytes as INTEGER)
    nonce_val = struct.pack(">Q", int.from_bytes(os.urandom(8), "big") & 0x7FFFFFFFFFFFFFFF)
    nonce = b"\x02" + bytes([len(nonce_val)]) + nonce_val
    # certReq: BOOLEAN TRUE
    cert_req = b"\x01\x01\xff"
    # version: INTEGER 1
    version = b"\x02\x01\x01"
    # TimeStampReq: SEQUENCE
    body = version + msg_imprint + nonce + cert_req
    tsq = b"\x30" + bytes([len(body)]) + body
    return tsq


class TSAClient:
    """Client for RFC 3161 timestamp requests to DigiCert TSA.

    Each request submits a SHA-256 hash and receives a signed
    timestamp token proving the hash existed at that time.
    """

    def __init__(self, url: str = DIGICERT_TSA_URL):
        self.url = url

    def request_timestamp(self, data_hash: str) -> Optional[str]:
        """Request a TSA timestamp for a hex hash digest.

        Returns base64-encoded TSA response token, or None on failure.
        Makes a direct HTTP request from the node — not through RETINA server.
        """
        try:
            import urllib.request

            digest = bytes.fromhex(data_hash)
            tsq = _build_tsa_request(digest)

            req = urllib.request.Request(
                self.url,
                data=tsq,
                headers={"Content-Type": "application/timestamp-query"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=15)
            tsr = resp.read()

            # Basic validation: response should start with ASN.1 SEQUENCE
            if tsr and tsr[0] == 0x30:
                token = base64.b64encode(tsr).decode("ascii")
                logger.debug("TSA token received (%d bytes)", len(tsr))
                return token
            else:
                logger.warning("TSA response invalid (not ASN.1 SEQUENCE)")
                return None

        except Exception as exc:
            logger.warning("TSA request failed: %s", exc)
            return None


class OpenTimestampsClient:
    """Client for OpenTimestamps calendar server submissions.

    Submits SHA-256 hashes for Bitcoin anchoring. Proofs are initially
    pending; the server upgrades them to full Bitcoin proofs asynchronously.
    """

    def __init__(self, calendar_urls: list[str] | None = None):
        self.calendar_urls = calendar_urls or OTS_CALENDAR_URLS

    def submit(self, data_hash: str) -> Optional[str]:
        """Submit a hash to OpenTimestamps calendar servers.

        Returns base64-encoded initial timestamp proof, or None on failure.
        Tries each calendar server in order until one succeeds.
        """
        try:
            import urllib.request

            digest = bytes.fromhex(data_hash)

            for url in self.calendar_urls:
                try:
                    submit_url = f"{url}/digest"
                    req = urllib.request.Request(
                        submit_url,
                        data=digest,
                        headers={"Content-Type": "application/x-opentimestamps"},
                        method="POST",
                    )
                    resp = urllib.request.urlopen(req, timeout=15)
                    proof = resp.read()
                    if proof:
                        encoded = base64.b64encode(proof).decode("ascii")
                        logger.debug("OTS proof received from %s (%d bytes)", url, len(proof))
                        return encoded
                except Exception as exc:
                    logger.debug("OTS submit to %s failed: %s", url, exc)
                    continue

            logger.warning("All OTS calendar servers failed")
            return None

        except Exception as exc:
            logger.warning("OTS submission failed: %s", exc)
            return None


class TimestampManager:
    """Manages both TSA and OTS timestamping for hash chain entries.

    Used by nodes to timestamp their hourly chain entries.
    Rate: one request per hour per node — well within free tier limits.
    """

    def __init__(self, enable_tsa: bool = True, enable_ots: bool = True):
        self.tsa = TSAClient() if enable_tsa else None
        self.ots = OpenTimestampsClient() if enable_ots else None

    def timestamp_entry(self, entry_hash: str) -> tuple[Optional[str], Optional[str]]:
        """Get TSA token and OTS proof for a hash chain entry.

        Returns (tsa_token, ots_proof) — either may be None if the
        service is unavailable.
        """
        tsa_token = None
        ots_proof = None

        if self.tsa:
            tsa_token = self.tsa.request_timestamp(entry_hash)

        if self.ots:
            ots_proof = self.ots.submit(entry_hash)

        return tsa_token, ots_proof
