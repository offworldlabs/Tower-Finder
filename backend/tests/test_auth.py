"""Tests for core/auth.py — JWT, user management, and FastAPI auth dependencies."""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest

from core.auth import (
    JWT_ALGORITHM,
    JWT_SECRET,
    create_token,
    get_all_users,
    get_or_create_user,
    update_user_role,
    verify_token,
)


# ── JWT Token Tests ───────────────────────────────────────────────────────────

class TestJWT:
    def test_create_and_verify_token(self):
        user = {"id": "u1", "email": "test@retina.fm", "role": "user"}
        token = create_token(user)
        payload = verify_token(token)
        assert payload is not None
        assert payload["sub"] == "u1"
        assert payload["email"] == "test@retina.fm"
        assert payload["role"] == "user"

    def test_token_contains_expiry(self):
        user = {"id": "u1", "email": "test@retina.fm", "role": "admin"}
        token = create_token(user)
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert "exp" in payload
        assert payload["exp"] > time.time()
        assert payload["exp"] <= time.time() + 86400 * 7 + 10

    def test_expired_token_rejected(self):
        """A token with exp in the past must be rejected."""
        payload = {
            "sub": "u1",
            "email": "test@retina.fm",
            "role": "user",
            "exp": int(time.time()) - 100,  # already expired
        }
        token = pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert verify_token(token) is None

    def test_tampered_token_rejected(self):
        user = {"id": "u1", "email": "test@retina.fm", "role": "user"}
        token = create_token(user)
        # Flip a character in the *middle* of the signature. The last character
        # of a 43-char base64url HS256 signature uses only 4 of its 6 bits
        # (the bottom 2 are padding zeros), so swapping the last char can leave
        # the decoded bytes unchanged and the token would still verify. Middle
        # characters use all 6 bits, so any single-char change invalidates the
        # HMAC.
        parts = token.split(".")
        sig = parts[2]
        mid = len(sig) // 2
        tampered_c = "A" if sig[mid] != "A" else "B"
        tampered_sig = sig[:mid] + tampered_c + sig[mid + 1:]
        tampered_token = f"{parts[0]}.{parts[1]}.{tampered_sig}"
        assert verify_token(tampered_token) is None

    def test_wrong_secret_rejected(self):
        payload = {
            "sub": "u1",
            "email": "test@retina.fm",
            "role": "user",
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, "wrong-secret-key", algorithm=JWT_ALGORITHM)
        assert verify_token(token) is None

    def test_garbage_token_rejected(self):
        assert verify_token("not.a.valid.jwt") is None
        assert verify_token("") is None
        assert verify_token("abc") is None

    def test_admin_role_preserved(self):
        user = {"id": "a1", "email": "admin@retina.fm", "role": "admin"}
        token = create_token(user)
        payload = verify_token(token)
        assert payload["role"] == "admin"


# ── User Store Tests ──────────────────────────────────────────────────────────

class TestUserStore:
    @pytest.fixture(autouse=True)
    def _mock_users_file(self, tmp_path):
        """Redirect USERS_FILE to a temp path for every test."""
        self.users_path = tmp_path / "users.json"
        with patch("core.auth.USERS_FILE", self.users_path):
            yield

    def test_create_new_user(self):
        user = get_or_create_user("alice@retina.fm", "Alice", "", "google")
        assert user["email"] == "alice@retina.fm"
        assert user["name"] == "Alice"
        assert user["provider"] == "google"
        assert user["role"] == "user"
        assert "id" in user

    def test_user_id_deterministic(self):
        """Same email → same user_id (sha256 hash)."""
        u1 = get_or_create_user("bob@retina.fm", "Bob", "", "github")
        u2 = get_or_create_user("bob@retina.fm", "Bob2", "", "github")
        assert u1["id"] == u2["id"]

    def test_email_normalized_to_lowercase(self):
        u1 = get_or_create_user("Alice@RETINA.FM", "Alice", "", "google")
        assert u1["email"] == "alice@retina.fm"
        u2 = get_or_create_user("alice@retina.fm", "Alice", "", "google")
        assert u1["id"] == u2["id"]

    def test_returning_user_updates_name_and_avatar(self):
        get_or_create_user("carol@retina.fm", "Carol", "av1", "google")
        u2 = get_or_create_user("carol@retina.fm", "Carol Updated", "av2", "google")
        assert u2["name"] == "Carol Updated"
        assert u2["avatar"] == "av2"

    def test_admin_email_gets_admin_role(self):
        with patch("core.auth.ADMIN_EMAILS", {"admin@retina.fm"}):
            user = get_or_create_user("admin@retina.fm", "Admin", "", "google")
        assert user["role"] == "admin"

    def test_non_admin_email_gets_user_role(self):
        with patch("core.auth.ADMIN_EMAILS", {"admin@retina.fm"}):
            user = get_or_create_user("user@retina.fm", "User", "", "google")
        assert user["role"] == "user"

    def test_get_all_users(self):
        get_or_create_user("a@retina.fm", "A", "", "google")
        get_or_create_user("b@retina.fm", "B", "", "github")
        users = get_all_users()
        assert len(users) == 2
        emails = {u["email"] for u in users}
        assert emails == {"a@retina.fm", "b@retina.fm"}

    def test_update_role_to_admin(self):
        user = get_or_create_user("dave@retina.fm", "Dave", "", "google")
        updated = update_user_role(user["id"], "admin")
        assert updated is not None
        assert updated["role"] == "admin"

    def test_update_role_to_user(self):
        user = get_or_create_user("eve@retina.fm", "Eve", "", "google")
        update_user_role(user["id"], "admin")
        updated = update_user_role(user["id"], "user")
        assert updated["role"] == "user"

    def test_update_role_invalid_role_rejected(self):
        user = get_or_create_user("f@retina.fm", "F", "", "google")
        assert update_user_role(user["id"], "superadmin") is None
        assert update_user_role(user["id"], "") is None

    def test_update_role_nonexistent_user_returns_none(self):
        assert update_user_role("nonexistent-id123", "admin") is None

    def test_users_persisted_to_disk(self):
        get_or_create_user("persist@retina.fm", "Persist", "", "google")
        assert self.users_path.exists()
        data = json.loads(self.users_path.read_text())
        assert len(data) == 1


# ── FastAPI Dependency Tests ──────────────────────────────────────────────────

class TestAuthDependencies:
    def test_get_current_user_no_auth_returns_anonymous(self):
        """When AUTH_ENABLED=False, any request gets anonymous admin."""
        import asyncio
        from core.auth import get_current_user
        request = MagicMock()
        with patch("core.auth.AUTH_ENABLED", False):
            user = asyncio.run(get_current_user(request))
        assert user["role"] == "admin"
        assert user["id"] == "anonymous"

    def test_require_admin_no_auth_returns_anonymous(self):
        import asyncio
        from core.auth import require_admin
        request = MagicMock()
        with patch("core.auth.AUTH_ENABLED", False):
            user = asyncio.run(require_admin(request))
        assert user["role"] == "admin"

    def test_get_current_user_missing_cookie_raises_401(self):
        import asyncio
        from core.auth import get_current_user
        from fastapi import HTTPException
        request = MagicMock()
        request.cookies = {}
        with patch("core.auth.AUTH_ENABLED", True):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    def test_get_current_user_invalid_token_raises_401(self):
        import asyncio
        from core.auth import get_current_user
        from fastapi import HTTPException
        request = MagicMock()
        request.cookies = {"auth_token": "invalid.jwt.token"}
        with patch("core.auth.AUTH_ENABLED", True):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    def test_require_admin_non_admin_raises_403(self, tmp_path):
        import asyncio
        from core.auth import require_admin
        from fastapi import HTTPException

        users_path = tmp_path / "users.json"
        user = {"id": "u1", "email": "regular@retina.fm", "role": "user"}
        token = create_token(user)

        # Write user to file
        users_path.write_text(json.dumps({"u1": user}))

        request = MagicMock()
        request.cookies = {"auth_token": token}

        with patch("core.auth.AUTH_ENABLED", True), \
             patch("core.auth.USERS_FILE", users_path):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_admin(request))
        assert exc_info.value.status_code == 403
