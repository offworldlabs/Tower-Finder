"""Tests for auth system: fastapi-users JWT, invite/claim/ownership logic, and FastAPI deps."""

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── SQLite durability pragmas ─────────────────────────────────────────────────

class TestSqlitePragmas:
    """The users.db engine MUST run in WAL mode with safety pragmas.

    Without WAL, a crash mid-commit can leave the database file in a state
    that the next process can't read — and we'd lose every user, invite,
    claim code, and node-ownership record. This test exists so that
    accidentally removing the `_set_sqlite_pragmas` event listener fails
    loudly in CI rather than silently shipping to prod.
    """

    @pytest.mark.asyncio
    async def test_engine_uses_wal_and_safety_pragmas(self):
        from core.users import engine

        async with engine.connect() as conn:
            jm = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            sync = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
            fk = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
            busy = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()

        assert str(jm).lower() == "wal", f"journal_mode must be WAL, got {jm!r}"
        # synchronous=NORMAL is integer 1 in SQLite's PRAGMA reply
        assert int(sync) == 1, f"synchronous must be NORMAL (1), got {sync!r}"
        assert int(fk) == 1, f"foreign_keys must be ON (1), got {fk!r}"
        assert int(busy) >= 1000, f"busy_timeout must be ≥1000ms, got {busy!r}"


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
        with patch("core.users.AUTH_BYPASS", True):
            user = asyncio.run(get_current_user(request))
        assert user["role"] == "admin"
        assert user["id"] == "00000000-0000-0000-0000-000000000000"

    def test_require_admin_auth_disabled_returns_anonymous(self):
        from core.users import require_admin

        request = MagicMock()
        with patch("core.users.AUTH_BYPASS", True):
            user = asyncio.run(require_admin(request))
        assert user["role"] == "admin"

    def test_get_current_user_missing_cookie_raises_401(self):
        from fastapi import HTTPException
        from starlette.datastructures import State

        from core.users import get_current_user

        request = MagicMock()
        request.cookies = {}
        request.state = State()
        with patch("core.users.AUTH_BYPASS", False):
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
        with patch("core.users.AUTH_BYPASS", False):
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

        with patch("core.users.AUTH_BYPASS", False), \
             patch("core.users._read_user_from_request", _fake_read):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_admin(request))
        assert exc_info.value.status_code == 403


# ── Shared DB fixture ─────────────────────────────────────────────────────────

@pytest.fixture()
def clean_auth_tables():
    """Wipe all auth-related tables before a test that requests this fixture."""
    from sqlalchemy import delete

    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(Invite))
            await session.execute(delete(NodeOwner))
            await session.execute(delete(ClaimCode))
            await session.commit()

    asyncio.run(_setup())
    yield


# ── Invites ───────────────────────────────────────────────────────────────────

class TestInvites:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_create_and_list_invite(self):
        from core.auth import create_invite, list_invites

        inv = await create_invite("alice@example.com", "user", "admin-id")
        assert inv["email"] == "alice@example.com"
        assert inv["role"] == "user"
        assert inv["used_at"] is None
        invites = await list_invites()
        assert len(invites) == 1
        assert invites[0]["token"] == inv["token"]

    async def test_invite_invalid_role_rejected(self):
        from core.auth import create_invite

        with pytest.raises(ValueError):
            await create_invite("a@b.com", "owner", "x")

    async def test_invite_invalid_email_rejected(self):
        from core.auth import create_invite

        with pytest.raises(ValueError):
            await create_invite("not-an-email", "user", "x")

    async def test_revoke_invite(self):
        from core.auth import create_invite, list_invites, revoke_invite

        inv = await create_invite("a@b.com", "user", "x")
        assert await revoke_invite(inv["token"]) is True
        assert await list_invites() == []
        assert await revoke_invite(inv["token"]) is False

    async def test_consume_invite_for_email(self):
        from core.auth import consume_invite_for_email, create_invite

        await create_invite("bob@example.com", "admin", "admin-id")
        role = await consume_invite_for_email("bob@example.com")
        assert role == "admin"
        # Invite is now used — cannot be consumed again
        assert await consume_invite_for_email("bob@example.com") is None

    async def test_invite_email_match_is_case_insensitive(self):
        from core.auth import consume_invite_for_email, create_invite

        await create_invite("Carol@Example.com", "admin", "x")
        assert await consume_invite_for_email("carol@example.com") == "admin"

    async def test_invite_does_not_apply_to_other_emails(self):
        from core.auth import consume_invite_for_email, create_invite

        await create_invite("dave@example.com", "admin", "x")
        assert await consume_invite_for_email("eve@example.com") is None

    async def test_invite_does_not_downgrade(self):
        """A 'user' invite for an email must not affect an admin — role logic is caller's job."""
        from core.auth import consume_invite_for_email, create_invite

        await create_invite("admin2@example.com", "user", "attacker")
        role = await consume_invite_for_email("admin2@example.com")
        assert role == "user"  # invite returns what it says; caller decides whether to apply


# ── Claim codes & node ownership ──────────────────────────────────────────────

class TestClaimCodesAndOwnership:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_create_claim_code(self):
        from core.auth import create_claim_code, list_claim_codes

        rec = await create_claim_code("user-123")
        assert rec["user_id"] == "user-123"
        assert rec["used_at"] is None
        assert len(rec["code"]) == 12
        assert rec["code"] == rec["code"].upper()
        codes = await list_claim_codes("user-123")
        assert len(codes) == 1
        assert await list_claim_codes("other-user") == []

    async def test_consume_claim_code_assigns_ownership(self):
        from core.auth import (
            consume_claim_code,
            create_claim_code,
            get_node_owner,
            get_user_nodes,
        )

        rec = await create_claim_code("user-A")
        owner = await consume_claim_code(rec["code"], "node-42")
        assert owner == "user-A"
        assert await get_node_owner("node-42") == "user-A"
        assert await get_user_nodes("user-A") == ["node-42"]

    async def test_consume_claim_code_is_one_shot(self):
        from core.auth import consume_claim_code, create_claim_code

        rec = await create_claim_code("user-A")
        assert await consume_claim_code(rec["code"], "node-42") == "user-A"
        assert await consume_claim_code(rec["code"], "node-43") is None

    async def test_consume_unknown_code_returns_none(self):
        from core.auth import consume_claim_code

        assert await consume_claim_code("DOESNOTEXIST", "node-42") is None

    async def test_consume_expired_code_fails(self):
        from core.auth import consume_claim_code, create_claim_code
        from core.users import ClaimCode, async_session_maker

        rec = await create_claim_code("user-A")
        async with async_session_maker() as session:
            claim = await session.get(ClaimCode, rec["code"])
            claim.expires_at = time.time() - 60
            await session.commit()
        assert await consume_claim_code(rec["code"], "node-42") is None

    async def test_revoke_claim_code_owner_check(self):
        from core.auth import create_claim_code, revoke_claim_code

        rec = await create_claim_code("user-A")
        assert await revoke_claim_code(rec["code"], "user-B") is False
        assert await revoke_claim_code(rec["code"], "user-A") is True

    async def test_revoke_used_code_fails(self):
        from core.auth import consume_claim_code, create_claim_code, revoke_claim_code

        rec = await create_claim_code("user-A")
        await consume_claim_code(rec["code"], "node-42")
        assert await revoke_claim_code(rec["code"], "user-A") is False

    async def test_claim_code_cap_enforced(self):
        from core.auth import _MAX_ACTIVE_CLAIM_CODES_PER_USER, create_claim_code

        for _ in range(_MAX_ACTIVE_CLAIM_CODES_PER_USER):
            await create_claim_code("user-cap")
        with pytest.raises(ValueError, match="Maximum"):
            await create_claim_code("user-cap")

    async def test_set_and_clear_node_owner(self):
        from core.auth import get_node_owner, list_node_owners, set_node_owner

        await set_node_owner("node-1", "user-A")
        await set_node_owner("node-2", "user-B")
        assert await get_node_owner("node-1") == "user-A"
        assert await list_node_owners() == {"node-1": "user-A", "node-2": "user-B"}
        await set_node_owner("node-1", None)
        assert await get_node_owner("node-1") is None
        assert "node-1" not in await list_node_owners()

    async def test_already_owned_claim_ack_omits_user_id(self):
        """The already_owned CLAIM_ACK message must not leak ownership info."""
        from core.auth import get_node_owner, set_node_owner

        await set_node_owner("node-owned", "user-secret")
        assert await get_node_owner("node-owned") == "user-secret"

        response_msg = {
            "type": "CLAIM_ACK",
            "node_id": "node-owned",
            "note": "already_owned",
        }
        assert "user_id" not in response_msg
        assert response_msg["note"] == "already_owned"


# ── Migration tests ───────────────────────────────────────────────────────────

class TestMigration:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_migrate_invites_from_json(self, tmp_path):
        from core.auth import list_invites, migrate_json_to_db

        invite_file = tmp_path / "invites.json"
        invite_file.write_text(json.dumps({
            "test-token-abc": {
                "email": "User@Example.COM",
                "role": "admin",
                "created_by": "migrator",
                "created_at": 1000.0,
                "expires_at": 9999999999.0,
                "used_at": None,
            }
        }))

        with patch("core.auth.INVITES_FILE", invite_file), \
             patch("core.auth.NODE_OWNERS_FILE", tmp_path / "node_owners.json"), \
             patch("core.auth.CLAIM_CODES_FILE", tmp_path / "claim_codes.json"):
            await migrate_json_to_db()

        invites = await list_invites()
        assert len(invites) == 1
        assert invites[0]["token"] == "test-token-abc"
        assert invites[0]["email"] == "user@example.com"  # lowercased
        assert invites[0]["role"] == "admin"
        migrated = invite_file.with_suffix(".json.migrated")
        assert migrated.exists()
        assert not invite_file.exists()

    async def test_migrate_invites_corrupted_json_logs_and_skips(self, tmp_path):
        from core.auth import list_invites, migrate_json_to_db

        invite_file = tmp_path / "invites.json"
        invite_file.write_text("not valid json")

        with patch("core.auth.INVITES_FILE", invite_file), \
             patch("core.auth.NODE_OWNERS_FILE", tmp_path / "node_owners.json"), \
             patch("core.auth.CLAIM_CODES_FILE", tmp_path / "claim_codes.json"):
            await migrate_json_to_db()

        assert invite_file.exists()
        assert not invite_file.with_suffix(".json.migrated").exists()
        assert await list_invites() == []

    async def test_migrate_node_owners_from_json(self, tmp_path):
        from core.auth import get_node_owner, migrate_json_to_db

        node_owners_file = tmp_path / "node_owners.json"
        node_owners_file.write_text(json.dumps({"node-A": "user-X"}))

        with patch("core.auth.INVITES_FILE", tmp_path / "invites.json"), \
             patch("core.auth.NODE_OWNERS_FILE", node_owners_file), \
             patch("core.auth.CLAIM_CODES_FILE", tmp_path / "claim_codes.json"):
            await migrate_json_to_db()

        assert await get_node_owner("node-A") == "user-X"
        migrated = node_owners_file.with_suffix(".json.migrated")
        assert migrated.exists()
        assert not node_owners_file.exists()

    async def test_migrate_claim_codes_from_json(self, tmp_path):
        from core.auth import list_claim_codes, migrate_json_to_db

        claim_codes_file = tmp_path / "claim_codes.json"
        claim_codes_file.write_text(json.dumps({
            "ABCDEF123456": {
                "user_id": "user-migrate",
                "created_at": 1000.0,
                "expires_at": 9999999999.0,
                "used_at": None,
                "used_by_node_id": None,
            }
        }))

        with patch("core.auth.INVITES_FILE", tmp_path / "invites.json"), \
             patch("core.auth.NODE_OWNERS_FILE", tmp_path / "node_owners.json"), \
             patch("core.auth.CLAIM_CODES_FILE", claim_codes_file):
            await migrate_json_to_db()

        codes = await list_claim_codes("user-migrate")
        assert len(codes) == 1
        assert codes[0]["code"] == "ABCDEF123456"
        migrated = claim_codes_file.with_suffix(".json.migrated")
        assert migrated.exists()
        assert not claim_codes_file.exists()


# ── set_node_owner UPDATE branch ──────────────────────────────────────────────

class TestSetNodeOwnerUpdate:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_set_node_owner_updates_existing_owner(self):
        from core.auth import get_node_owner, set_node_owner

        await set_node_owner("migrate-node", "user-1")
        await set_node_owner("migrate-node", "user-2")
        assert await get_node_owner("migrate-node") == "user-2"


# ── revoke_claim_code edge cases ──────────────────────────────────────────────

class TestRevokeClaimCodeEdgeCases:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_revoke_nonexistent_code_returns_false(self):
        from core.auth import revoke_claim_code

        result = await revoke_claim_code("DOESNOTEXIST")
        assert result is False


# ── consume_claim_code edge cases ─────────────────────────────────────────────

class TestConsumeClaimCodeEdgeCases:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_consume_empty_code_returns_none(self):
        from core.auth import consume_claim_code

        result = await consume_claim_code("", "node-X")
        assert result is None

    async def test_consume_empty_node_returns_none(self):
        from core.auth import consume_claim_code

        result = await consume_claim_code("SOMECODE", "")
        assert result is None

    async def test_consume_updates_existing_node_owner(self):
        from core.auth import (
            consume_claim_code,
            create_claim_code,
            get_node_owner,
            set_node_owner,
        )

        await set_node_owner("node-X", "old-user")
        rec = await create_claim_code("new-user")
        result = await consume_claim_code(rec["code"], "node-X")
        assert result == "new-user"
        assert await get_node_owner("node-X") == "new-user"
