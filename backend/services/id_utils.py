"""Identifier utilities shared across service modules."""

import hashlib


def multinode_hex_from_key(key: str) -> str:
    """Return deterministic synthetic hex ID for a multinode solve key."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:10]
    return f"mn{digest}"
