"""Authentication helpers — JWT tokens, user store, FastAPI dependencies."""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import jwt  # PyJWT

logger = logging.getLogger(__name__)

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()

_jwt_from_env = os.getenv("JWT_SECRET", "")
if not _jwt_from_env and _RETINA_ENV not in ("dev", "test"):
    raise RuntimeError(
        "JWT_SECRET environment variable is required in production "
        f"(RETINA_ENV={_RETINA_ENV!r}). Set it to a random ≥32-byte string."
    )
JWT_SECRET = _jwt_from_env or "retina-dev-secret-change-me-in-prod-32b!"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY = 86400 * 7  # 7 days

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USERS_FILE = _DATA_DIR / "users.json"
INVITES_FILE = _DATA_DIR / "invites.json"
NODE_OWNERS_FILE = _DATA_DIR / "node_owners.json"
CLAIM_CODES_FILE = _DATA_DIR / "claim_codes.json"

INVITE_EXPIRY_S = 86400 * 14   # 14 days
CLAIM_CODE_EXPIRY_S = 86400 * 30  # 30 days

ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("AUTH_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Auth is disabled when no OAuth keys are configured
AUTH_ENABLED = bool(os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GITHUB_CLIENT_ID"))

_ANONYMOUS_USER = {
    "id": "anonymous",
    "email": "admin@retina.fm",
    "name": "Admin (no auth)",
    "avatar": "",
    "provider": "none",
    "role": "admin",
    "created_at": 0,
    "last_login": 0,
}

if not AUTH_ENABLED:
    logger.warning("AUTH DISABLED — no GOOGLE_CLIENT_ID or GITHUB_CLIENT_ID set. "
                   "All requests get full admin access.")

_lock = threading.Lock()


# ── User store (JSON file) ───────────────────────────────────────────────────

def _load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}


def _save_users(users: dict):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))


# ── Invites (admin-issued, matched on first SSO login by email) ──────────────

def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Corrupt JSON store: %s", path)
            return {}
    return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def create_invite(email: str, role: str, created_by: str) -> dict:
    """Create an admin-issued invite for an email. Returns the invite record."""
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
        invites = _load_json(INVITES_FILE)
    return list(invites.values())


def revoke_invite(token: str) -> bool:
    with _lock:
        invites = _load_json(INVITES_FILE)
        if token not in invites:
            return False
        del invites[token]
        _save_json(INVITES_FILE, invites)
    return True


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


# ── Node ownership ───────────────────────────────────────────────────────────

def get_node_owner(node_id: str) -> str | None:
    with _lock:
        owners = _load_json(NODE_OWNERS_FILE)
    return owners.get(node_id)


def list_node_owners() -> dict[str, str]:
    with _lock:
        return _load_json(NODE_OWNERS_FILE)


def set_node_owner(node_id: str, user_id: str | None) -> None:
    """Assign or clear node ownership. Pass user_id=None to unassign."""
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


# ── Claim codes (user-issued, used by node in HELLO to self-claim) ────────────

def create_claim_code(user_id: str) -> dict:
    """Create a one-time claim code for the user."""
    now = time.time()
    # 8-char base32-style code, easy to type
    code = secrets.token_hex(4).upper()
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
        codes[code] = record
        _save_json(CLAIM_CODES_FILE, codes)
    return record


def list_claim_codes(user_id: str | None = None) -> list[dict]:
    """List all claim codes, or only those belonging to a user."""
    with _lock:
        codes = _load_json(CLAIM_CODES_FILE)
    out = list(codes.values())
    if user_id is not None:
        out = [c for c in out if c.get("user_id") == user_id]
    return out


def revoke_claim_code(code: str, user_id: str | None = None) -> bool:
    """Delete an unused claim code. If user_id supplied, only revoke if owner matches."""
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
    """Mark a claim code as used and assign node ownership.

    Returns the user_id that now owns the node, or None if the code is invalid,
    expired, or already consumed.
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
        rec["used_at"] = now
        rec["used_by_node_id"] = node_id
        _save_json(CLAIM_CODES_FILE, codes)
        owners = _load_json(NODE_OWNERS_FILE)
        owners[node_id] = rec["user_id"]
        _save_json(NODE_OWNERS_FILE, owners)
    return rec["user_id"]


def get_or_create_user(email: str, name: str, avatar: str, provider: str) -> dict:
    with _lock:
        users = _load_users()
        user_id = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
        now = time.time()
        if user_id not in users:
            # Check if there's a pending admin invite for this email
            invited_role = _consume_invite_for_email_locked(email)
            default_role = "admin" if email.lower() in ADMIN_EMAILS else "user"
            users[user_id] = {
                "id": user_id,
                "email": email.lower(),
                "name": name,
                "avatar": avatar,
                "provider": provider,
                "role": invited_role or default_role,
                "created_at": now,
                "last_login": now,
            }
            logger.info("Created new user: %s (%s, role=%s, via_invite=%s)",
                        email, provider, users[user_id]["role"], invited_role is not None)
        else:
            users[user_id]["name"] = name
            users[user_id]["avatar"] = avatar
            users[user_id]["last_login"] = now
            # Allow a pending invite to upgrade role on subsequent login
            invited_role = _consume_invite_for_email_locked(email)
            if invited_role and users[user_id].get("role") != invited_role:
                users[user_id]["role"] = invited_role
                logger.info("Updated %s role to %s via pending invite", email, invited_role)
        _save_users(users)
        return users[user_id]


def get_all_users() -> list[dict]:
    with _lock:
        users = _load_users()
    return list(users.values())


def get_user_by_id(user_id: str) -> dict | None:
    with _lock:
        users = _load_users()
    return users.get(user_id)


def update_user_role(user_id: str, role: str) -> dict | None:
    if role not in ("user", "admin"):
        return None
    with _lock:
        users = _load_users()
        if user_id not in users:
            return None
        users[user_id]["role"] = role
        _save_users(users)
        return users[user_id]


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_token(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "role": user["role"],
        "exp": int(time.time()) + JWT_EXPIRY,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError):
        return None


# ── FastAPI dependencies ──────────────────────────────────────────────────────

from fastapi import HTTPException, Request  # noqa: E402


async def get_current_user(request: Request) -> dict:
    """Extract and validate auth cookie. Returns user dict or raises 401."""
    if not AUTH_ENABLED:
        return _ANONYMOUS_USER
    token = request.cookies.get("auth_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    with _lock:
        users = _load_users()
    user = users.get(payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def require_admin(request: Request) -> dict:
    """Like get_current_user but also enforces admin role."""
    if not AUTH_ENABLED:
        return _ANONYMOUS_USER
    user = await get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
