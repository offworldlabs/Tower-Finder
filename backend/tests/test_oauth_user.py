"""Tests for get_or_create_oauth_user() in core/users.py.

Covers new-user creation, profile updates on re-login, invite-based role
upgrades, email normalisation, and idempotency of repeated calls.
"""

import asyncio

import pytest

from core.users import User, async_session_maker, create_db_and_tables, get_or_create_oauth_user

# ── helpers ───────────────────────────────────────────────────────────────────


async def _no_invite(email: str):
    """Consume-invite stub that always returns None (no pending invite)."""
    return None


async def _admin_invite(email: str):
    """Consume-invite stub that always returns "admin"."""
    return "admin"


async def _create_user(
    email: str,
    *,
    name: str = "Test",
    avatar: str = "",
    provider: str = "google",
    superuser: bool = False,
) -> User:
    """Pre-create a user so "existing user" test cases have something to work with."""

    async def _invite(e: str):
        return "admin" if superuser else None

    return await get_or_create_oauth_user(
        email=email,
        name=name,
        avatar=avatar,
        provider=provider,
        consume_invite_fn=_invite,
    )


# ── test class ────────────────────────────────────────────────────────────────


class TestGetOrCreateOauthUser:
    """Integration tests against a real (in-process) SQLite DB."""

    @pytest.fixture(autouse=True)
    def _clean_users(self):
        """Truncate the User table before each test.

        The global _clean_db autouse fixture already handles Invite/NodeOwner/
        ClaimCode and calls create_db_and_tables(), but it leaves Users intact.
        We truncate here so tests don't bleed state into each other.
        """
        from sqlalchemy import delete

        async def _setup():
            await create_db_and_tables()
            async with async_session_maker() as session:
                await session.execute(delete(User))
                await session.commit()

        # asyncio.run() calls set_event_loop(None) on exit (Python 3.12).
        # Restore a fresh loop so pytest-asyncio 0.23.x can call
        # get_event_loop() before handing control to each async test.
        asyncio.run(_setup())
        asyncio.set_event_loop(asyncio.new_event_loop())
        yield

    # 1 ── new user is created ─────────────────────────────────────────────────

    async def test_new_user_is_created(self):
        user = await get_or_create_oauth_user(
            email="alice@example.com",
            name="Alice",
            avatar="http://avatar",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert user.email == "alice@example.com"

    # 2 ── new user is not superuser by default ────────────────────────────────

    async def test_new_user_is_not_superuser_by_default(self):
        user = await get_or_create_oauth_user(
            email="bob@example.com",
            name="Bob",
            avatar="",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert user.is_superuser is False

    # 3 ── new user with admin invite becomes superuser ────────────────────────

    async def test_new_user_with_admin_invite_is_superuser(self):
        user = await get_or_create_oauth_user(
            email="carol@example.com",
            name="Carol",
            avatar="",
            provider="google",
            consume_invite_fn=_admin_invite,
        )
        assert user.is_superuser is True

    # 4 ── existing user: profile fields are updated on re-login ──────────────

    async def test_existing_user_profile_updated_on_relogin(self):
        await _create_user("dave@example.com", name="Old Name", avatar="http://old-avatar")

        updated = await get_or_create_oauth_user(
            email="dave@example.com",
            name="New Name",
            avatar="http://new-avatar",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert updated.name == "New Name"
        assert updated.avatar == "http://new-avatar"

    # 5 ── existing regular user is upgraded to admin via invite ──────────────

    async def test_existing_user_role_upgrade_on_admin_invite(self):
        await _create_user("eve@example.com", superuser=False)

        upgraded = await get_or_create_oauth_user(
            email="eve@example.com",
            name="Eve",
            avatar="",
            provider="google",
            consume_invite_fn=_admin_invite,
        )
        assert upgraded.is_superuser is True

    # 6 ── existing superuser is NOT downgraded when no invite is present ──────

    async def test_existing_user_not_downgraded(self):
        await _create_user("frank@example.com", superuser=True)

        still_admin = await get_or_create_oauth_user(
            email="frank@example.com",
            name="Frank",
            avatar="",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert still_admin.is_superuser is True

    # 7 ── email is normalised to lowercase ───────────────────────────────────

    async def test_email_normalized_to_lowercase(self):
        user = await get_or_create_oauth_user(
            email="USER@EXAMPLE.COM",
            name="Upper",
            avatar="",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert user.email == "user@example.com"

    # 8 ── two calls with the same email return the same user id ──────────────

    async def test_second_call_returns_same_user_id(self):
        first = await get_or_create_oauth_user(
            email="grace@example.com",
            name="Grace",
            avatar="",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        second = await get_or_create_oauth_user(
            email="grace@example.com",
            name="Grace Again",
            avatar="",
            provider="google",
            consume_invite_fn=_no_invite,
        )
        assert first.id == second.id
