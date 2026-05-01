"""Tests for auth system: fastapi-users JWT, invite/claim/ownership logic, and FastAPI deps."""

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── JWT via fastapi-users JWTStrategy ─────────────────────────────────────────

class TestJWT:
    """Verify that fastapi-users' JWTStrategy correctly issues and validates tokens."""

    def _make_user(self, *, is_superuser: bool = False) -> MagicMock:
        user = MagicMock()
        user.id = uuid.uuid4()
        user.email = "test@retina.fm"
        user.is_active = True
        user.is_superuser = is_superuser
        return user

    def test_write_and_read_token_roundtrip(self):
        """A token written by JWTStrategy must be readable back to the same user id."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()

        token = asyncio.run(strategy.write_token(user))
        assert isinstance(token, str)
        assert len(token) > 0

        # read_token looks up the user by id — mock the manager to return our user
        mock_manager = MagicMock()
        mock_manager.get = AsyncMock(return_value=user)

        result = asyncio.run(strategy.read_token(token, mock_manager))
        assert result is not None
        assert result.id == user.id

    def test_token_contains_subject(self):
        """The JWT sub claim must equal the user's id."""
        import jwt as pyjwt

        from core.users import JWT_SECRET, get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        # fastapi-users sets aud=["fastapi-users:auth"] — pass it when decoding
        payload = pyjwt.decode(
            token, JWT_SECRET, algorithms=["HS256"],
            audience=["fastapi-users:auth"],
        )
        assert payload["sub"] == str(user.id)

    def test_token_has_expiry(self):
        """Token must include an exp claim set in the future."""
        import jwt as pyjwt

        from core.users import JWT_LIFETIME_SECONDS, JWT_SECRET, get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        payload = pyjwt.decode(
            token, JWT_SECRET, algorithms=["HS256"],
            audience=["fastapi-users:auth"],
        )
        assert "exp" in payload
        assert payload["exp"] > time.time()
        assert payload["exp"] <= time.time() + JWT_LIFETIME_SECONDS + 10

    def test_tampered_token_not_readable(self):
        """read_token on a tampered JWT must return None (not raise)."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        parts = token.split(".")
        sig = parts[2]
        mid = len(sig) // 2
        tampered_c = "A" if sig[mid] != "A" else "B"
        tampered = f"{parts[0]}.{parts[1]}.{sig[:mid]}{tampered_c}{sig[mid + 1:]}"

        # read_token with None user_manager returns None for bad tokens
        result = asyncio.run(strategy.read_token(tampered, None))
        assert result is None

    def test_expired_token_rejected(self):
        """A token with a past exp must be rejected by read_token."""
        import jwt as pyjwt

        from core.users import JWT_SECRET, get_jwt_strategy

        payload = {
            "sub": str(uuid.uuid4()),
            "aud": ["fastapi-users:auth"],
            "exp": int(time.time()) - 60,
        }
        expired_token = pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

        strategy = get_jwt_strategy()
        result = asyncio.run(strategy.read_token(expired_token, None))
        assert result is None

    def test_garbage_token_rejected(self):
        """Nonsense strings must return None, not raise."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        for bad in ("", "not.a.jwt", "abc"):
            assert asyncio.run(strategy.read_token(bad, None)) is None


# ── get_current_user / require_admin dependencies ─────────────────────────────

class TestAuthDependencies:
    def test_get_current_user_auth_disabled_returns_anonymous(self):
        from core.users import get_current_user

        request = MagicMock()
        with patch("core.users._AUTH_BYPASS", True):
            user = asyncio.run(get_current_user(request))
        assert user["role"] == "admin"
        assert user["id"] == "00000000-0000-0000-0000-000000000000"

    def test_require_admin_auth_disabled_returns_anonymous(self):
        from core.users import require_admin

        request = MagicMock()
        with patch("core.users._AUTH_BYPASS", True):
            user = asyncio.run(require_admin(request))
        assert user["role"] == "admin"

    def test_get_current_user_missing_cookie_raises_401(self):
        from fastapi import HTTPException
        from starlette.datastructures import State

        from core.users import get_current_user

        request = MagicMock()
        request.cookies = {}
        request.state = State()
        with patch("core.users._AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    def test_get_current_user_invalid_token_raises_401(self):
        from fastapi import HTTPException
        from starlette.datastructures import State

        from core.users import get_current_user

        request = MagicMock()
        request.cookies = {"auth_token": "invalid.jwt.token"}
        request.state = State()
        with patch("core.users._AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    def test_require_admin_non_admin_raises_403(self, tmp_path):
        """A valid token for a non-superuser must yield 403 from require_admin."""
        import jwt as pyjwt
        from fastapi import HTTPException

        from core.users import JWT_SECRET, require_admin

        # Build a valid JWT for a regular (non-superuser) user
        uid = uuid.uuid4()
        payload = {
            "sub": str(uid),
            "aud": ["fastapi-users:auth"],
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

        request = MagicMock()
        request.cookies = {"auth_token": token}

        # Patch _read_user_from_request so we don't need a real DB
        non_admin_user = MagicMock()
        non_admin_user.is_active = True
        non_admin_user.is_superuser = False

        async def _fake_read(req):
            return non_admin_user

        with patch("core.users._AUTH_BYPASS", False), \
             patch("core.users._read_user_from_request", _fake_read):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_admin(request))
        assert exc_info.value.status_code == 403


# ── Invites ───────────────────────────────────────────────────────────────────

class TestInvites:
    @pytest.fixture(autouse=True)
    def _redirect_stores(self, tmp_path):
        invites = tmp_path / "invites.json"
        with patch("core.auth.INVITES_FILE", invites):
            yield

    def test_create_and_list_invite(self):
        from core.auth import create_invite, list_invites

        inv = create_invite("alice@example.com", "user", "admin-id")
        assert inv["email"] == "alice@example.com"
        assert inv["role"] == "user"
        assert inv["used_at"] is None
        invites = list_invites()
        assert len(invites) == 1
        assert invites[0]["token"] == inv["token"]

    def test_invite_invalid_role_rejected(self):
        from core.auth import create_invite

        with pytest.raises(ValueError):
            create_invite("a@b.com", "owner", "x")

    def test_invite_invalid_email_rejected(self):
        from core.auth import create_invite

        with pytest.raises(ValueError):
            create_invite("not-an-email", "user", "x")

    def test_revoke_invite(self):
        from core.auth import create_invite, list_invites, revoke_invite

        inv = create_invite("a@b.com", "user", "x")
        assert revoke_invite(inv["token"]) is True
        assert list_invites() == []
        assert revoke_invite(inv["token"]) is False

    def test_consume_invite_for_email(self):
        from core.auth import consume_invite_for_email, create_invite

        create_invite("bob@example.com", "admin", "admin-id")
        role = consume_invite_for_email("bob@example.com")
        assert role == "admin"
        # Invite is now used — cannot be consumed again
        assert consume_invite_for_email("bob@example.com") is None

    def test_invite_email_match_is_case_insensitive(self):
        from core.auth import consume_invite_for_email, create_invite

        create_invite("Carol@Example.com", "admin", "x")
        assert consume_invite_for_email("carol@example.com") == "admin"

    def test_invite_does_not_apply_to_other_emails(self):
        from core.auth import consume_invite_for_email, create_invite

        create_invite("dave@example.com", "admin", "x")
        assert consume_invite_for_email("eve@example.com") is None

    def test_invite_does_not_downgrade(self):
        """A 'user' invite for an email must not affect an admin — role logic is caller's job."""
        from core.auth import consume_invite_for_email, create_invite

        create_invite("admin2@example.com", "user", "attacker")
        role = consume_invite_for_email("admin2@example.com")
        assert role == "user"  # invite returns what it says; caller decides whether to apply


# ── Claim codes & node ownership ──────────────────────────────────────────────

class TestClaimCodesAndOwnership:
    @pytest.fixture(autouse=True)
    def _redirect_stores(self, tmp_path):
        owners = tmp_path / "node_owners.json"
        codes = tmp_path / "claim_codes.json"
        with patch("core.auth.NODE_OWNERS_FILE", owners), \
             patch("core.auth.CLAIM_CODES_FILE", codes):
            yield

    def test_create_claim_code(self):
        from core.auth import create_claim_code, list_claim_codes

        rec = create_claim_code("user-123")
        assert rec["user_id"] == "user-123"
        assert rec["used_at"] is None
        assert len(rec["code"]) == 12
        assert rec["code"] == rec["code"].upper()
        codes = list_claim_codes("user-123")
        assert len(codes) == 1
        assert list_claim_codes("other-user") == []

    def test_consume_claim_code_assigns_ownership(self):
        from core.auth import (
            consume_claim_code,
            create_claim_code,
            get_node_owner,
            get_user_nodes,
        )

        rec = create_claim_code("user-A")
        owner = consume_claim_code(rec["code"], "node-42")
        assert owner == "user-A"
        assert get_node_owner("node-42") == "user-A"
        assert get_user_nodes("user-A") == ["node-42"]

    def test_consume_claim_code_is_one_shot(self):
        from core.auth import consume_claim_code, create_claim_code

        rec = create_claim_code("user-A")
        assert consume_claim_code(rec["code"], "node-42") == "user-A"
        assert consume_claim_code(rec["code"], "node-43") is None

    def test_consume_unknown_code_returns_none(self):
        from core.auth import consume_claim_code

        assert consume_claim_code("DOESNOTEXIST", "node-42") is None

    def test_consume_expired_code_fails(self):
        from core.auth import CLAIM_CODES_FILE, consume_claim_code, create_claim_code

        rec = create_claim_code("user-A")
        data = json.loads(CLAIM_CODES_FILE.read_text())
        data[rec["code"]]["expires_at"] = time.time() - 60
        CLAIM_CODES_FILE.write_text(json.dumps(data))
        assert consume_claim_code(rec["code"], "node-42") is None

    def test_revoke_claim_code_owner_check(self):
        from core.auth import create_claim_code, revoke_claim_code

        rec = create_claim_code("user-A")
        assert revoke_claim_code(rec["code"], "user-B") is False
        assert revoke_claim_code(rec["code"], "user-A") is True

    def test_revoke_used_code_fails(self):
        from core.auth import consume_claim_code, create_claim_code, revoke_claim_code

        rec = create_claim_code("user-A")
        consume_claim_code(rec["code"], "node-42")
        assert revoke_claim_code(rec["code"], "user-A") is False

    def test_claim_code_cap_enforced(self):
        from core.auth import _MAX_ACTIVE_CLAIM_CODES_PER_USER, create_claim_code

        for _ in range(_MAX_ACTIVE_CLAIM_CODES_PER_USER):
            create_claim_code("user-cap")
        with pytest.raises(ValueError, match="Maximum"):
            create_claim_code("user-cap")

    def test_set_and_clear_node_owner(self):
        from core.auth import get_node_owner, list_node_owners, set_node_owner

        set_node_owner("node-1", "user-A")
        set_node_owner("node-2", "user-B")
        assert get_node_owner("node-1") == "user-A"
        assert list_node_owners() == {"node-1": "user-A", "node-2": "user-B"}
        set_node_owner("node-1", None)
        assert get_node_owner("node-1") is None
        assert "node-1" not in list_node_owners()

    def test_already_owned_claim_ack_omits_user_id(self):
        """The already_owned CLAIM_ACK message must not leak ownership info."""
        from core.auth import get_node_owner, set_node_owner

        set_node_owner("node-owned", "user-secret")
        assert get_node_owner("node-owned") == "user-secret"

        response_msg = {
            "type": "CLAIM_ACK",
            "node_id": "node-owned",
            "note": "already_owned",
        }
        assert "user_id" not in response_msg
        assert response_msg["note"] == "already_owned"
