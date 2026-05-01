"""Domain-specific auth helpers: invites, node ownership, claim codes.

JWT, user storage, and session management are handled by fastapi-users
(see core/users.py). This module contains only the business logic that
has no equivalent in a general-purpose auth library.
"""

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVITES_FILE = _DATA_DIR / "invites.json"
NODE_OWNERS_FILE = _DATA_DIR / "node_owners.json"
CLAIM_CODES_FILE = _DATA_DIR / "claim_codes.json"

INVITE_EXPIRY_S = 86400 * 14   # 14 days
CLAIM_CODE_EXPIRY_S = 86400 * 30  # 30 days

_MAX_ACTIVE_CLAIM_CODES_PER_USER = 10

_lock = threading.Lock()


# ── JSON file helpers ─────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Corrupt JSON store: %s", path)
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ── Invites ───────────────────────────────────────────────────────────────────

def create_invite(email: str, role: str, created_by: str) -> dict:
    if role not in ("user", "admin"):
        raise ValueError("invalid role")
    email = email.lower().strip()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    now = time.time()
    token = secrets.token_urlsafe(16)
    invite = {
        "token": token,
        "email": email,
        "role": role,
        "created_by": created_by,
        "created_at": now,
        "expires_at": now + INVITE_EXPIRY_S,
        "used_at": None,
    }
    with _lock:
        invites = _load_json(INVITES_FILE)
        invites[token] = invite
        _save_json(INVITES_FILE, invites)
    return invite


def list_invites() -> list[dict]:
    with _lock:
        return list(_load_json(INVITES_FILE).values())


def revoke_invite(token: str) -> bool:
    with _lock:
        invites = _load_json(INVITES_FILE)
        if token not in invites:
            return False
        del invites[token]
        _save_json(INVITES_FILE, invites)
    return True


def consume_invite_for_email(email: str) -> str | None:
    """Consume the oldest valid invite for this email. Returns role or None.

    Caller must NOT hold _lock — this function acquires it internally.
    Used from the OAuth callback (async context) via get_or_create_oauth_user.
    """
    with _lock:
        return _consume_invite_for_email_locked(email)


def _consume_invite_for_email_locked(email: str) -> str | None:
    """Caller must hold _lock. Returns role or None. Marks invite used."""
    invites = _load_json(INVITES_FILE)
    now = time.time()
    matched_token = None
    for tok, inv in invites.items():
        if inv.get("used_at") is not None:
            continue
        if inv.get("expires_at", 0) < now:
            continue
        if inv.get("email", "").lower() == email.lower():
            matched_token = tok
            break
    if matched_token is None:
        return None
    invites[matched_token]["used_at"] = now
    role = invites[matched_token]["role"]
    _save_json(INVITES_FILE, invites)
    return role


# ── Node ownership ────────────────────────────────────────────────────────────

def get_node_owner(node_id: str) -> str | None:
    with _lock:
        return _load_json(NODE_OWNERS_FILE).get(node_id)


def list_node_owners() -> dict[str, str]:
    with _lock:
        return _load_json(NODE_OWNERS_FILE)


def set_node_owner(node_id: str, user_id: str | None) -> None:
    with _lock:
        owners = _load_json(NODE_OWNERS_FILE)
        if user_id is None:
            owners.pop(node_id, None)
        else:
            owners[node_id] = user_id
        _save_json(NODE_OWNERS_FILE, owners)


def get_user_nodes(user_id: str) -> list[str]:
    with _lock:
        owners = _load_json(NODE_OWNERS_FILE)
    return [nid for nid, uid in owners.items() if uid == user_id]


# ── Claim codes ───────────────────────────────────────────────────────────────

def create_claim_code(user_id: str) -> dict:
    """Create a one-time claim code for the user.

    Raises ValueError if the user already has _MAX_ACTIVE_CLAIM_CODES_PER_USER
    active (unused, non-expired) codes.
    """
    now = time.time()
    code = secrets.token_hex(6).upper()  # 12 hex chars = 48 bits of entropy
    record = {
        "code": code,
        "user_id": user_id,
        "created_at": now,
        "expires_at": now + CLAIM_CODE_EXPIRY_S,
        "used_at": None,
        "used_by_node_id": None,
    }
    with _lock:
        codes = _load_json(CLAIM_CODES_FILE)
        active = [
            c for c in codes.values()
            if c.get("user_id") == user_id
            and c.get("used_at") is None
            and c.get("expires_at", 0) >= now
        ]
        if len(active) >= _MAX_ACTIVE_CLAIM_CODES_PER_USER:
            raise ValueError(
                f"Maximum of {_MAX_ACTIVE_CLAIM_CODES_PER_USER} active claim codes "
                "allowed per user. Revoke an existing code first."
            )
        codes[code] = record
        _save_json(CLAIM_CODES_FILE, codes)
    return record


def list_claim_codes(user_id: str | None = None) -> list[dict]:
    with _lock:
        codes = _load_json(CLAIM_CODES_FILE)
    out = list(codes.values())
    if user_id is not None:
        out = [c for c in out if c.get("user_id") == user_id]
    return out


def revoke_claim_code(code: str, user_id: str | None = None) -> bool:
    with _lock:
        codes = _load_json(CLAIM_CODES_FILE)
        rec = codes.get(code)
        if not rec:
            return False
        if user_id is not None and rec.get("user_id") != user_id:
            return False
        if rec.get("used_at") is not None:
            return False
        del codes[code]
        _save_json(CLAIM_CODES_FILE, codes)
    return True


def consume_claim_code(code: str, node_id: str) -> str | None:
    """Mark a claim code used and assign node ownership atomically.

    Returns the user_id that now owns the node, or None on any failure.
    Rolls back the used_at mark if the ownership write fails so the code
    can be retried — keeps both JSON stores consistent under crash/disk-full.
    """
    if not code or not node_id:
        return None
    code = code.strip().upper()
    now = time.time()
    with _lock:
        codes = _load_json(CLAIM_CODES_FILE)
        rec = codes.get(code)
        if not rec:
            return None
        if rec.get("used_at") is not None:
            return None
        if rec.get("expires_at", 0) < now:
            return None
        owners = _load_json(NODE_OWNERS_FILE)
        owner_user_id = rec["user_id"]
        owners[node_id] = owner_user_id
        rec["used_at"] = now
        rec["used_by_node_id"] = node_id
        try:
            _save_json(NODE_OWNERS_FILE, owners)
        except Exception:
            rec["used_at"] = None
            rec["used_by_node_id"] = None
            raise
        _save_json(CLAIM_CODES_FILE, codes)
    return owner_user_id
