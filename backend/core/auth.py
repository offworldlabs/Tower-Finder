"""Authentication helpers — JWT tokens, user store, FastAPI dependencies."""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import jwt  # PyJWT

logger = logging.getLogger(__name__)

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()

_jwt_from_env = os.getenv("JWT_SECRET", "")
if not _jwt_from_env and _RETINA_ENV not in ("dev", "test", ""):
    raise RuntimeError(
        "JWT_SECRET environment variable is required in production "
        f"(RETINA_ENV={_RETINA_ENV!r}). Set it to a random ≥32-byte string."
    )
JWT_SECRET = _jwt_from_env or "retina-dev-secret-change-me-in-prod-32b!"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY = 86400 * 7  # 7 days

USERS_FILE = Path(__file__).resolve().parent.parent / "data" / "users.json"
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


def get_or_create_user(email: str, name: str, avatar: str, provider: str) -> dict:
    with _lock:
        users = _load_users()
        user_id = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
        now = time.time()
        if user_id not in users:
            users[user_id] = {
                "id": user_id,
                "email": email.lower(),
                "name": name,
                "avatar": avatar,
                "provider": provider,
                "role": "admin" if email.lower() in ADMIN_EMAILS else "user",
                "created_at": now,
                "last_login": now,
            }
            logger.info("Created new user: %s (%s)", email, provider)
        else:
            users[user_id]["name"] = name
            users[user_id]["avatar"] = avatar
            users[user_id]["last_login"] = now
        _save_users(users)
        return users[user_id]


def get_all_users() -> list[dict]:
    with _lock:
        users = _load_users()
    return list(users.values())


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

from fastapi import Request, HTTPException  # noqa: E402


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
