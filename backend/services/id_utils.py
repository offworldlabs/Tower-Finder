"""Identifier utilities shared across service modules."""

import hashlib


def multinode_hex_from_key(key: str) -> str:
    """Return deterministic synthetic hex ID for a multinode solve key."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:10]
    return f"mn{digest}"


def normalize_hex_key(hex_code) -> str:
    """Canonical form for an ICAO hex used as a dict key: stripped, lowercased.

    Every path that writes ``state.adsb_aircraft`` (or cross-references it)
    must go through this — readers that dedupe lowercase first, so a raw
    uppercase key is a silent lookup miss, not an error.
    """
    return str(hex_code or "").strip().lower()
