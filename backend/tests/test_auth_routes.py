"""HTTP endpoint tests for auth routes.

Covers:
  /api/auth/me, /api/auth/logout
  /api/auth/me/claim-codes  (GET / POST / DELETE)
  /api/auth/me/nodes
  OAuth state token (CSRF + open-redirect guards)
  /api/admin/invites         (GET / POST / DELETE)
  /api/admin/node-owners     (GET)
  /api/admin/nodes/{id}/owner (PUT)
"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_db():
    from sqlalchemy import delete
    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(ClaimCode))
            await session.execute(delete(NodeOwner))
            await session.execute(delete(Invite))
            await session.commit()

    asyncio.run(_setup())
    yield


# ── /api/auth/me + /api/auth/logout ──────────────────────────────────────────

class TestMeEndpoint:
    def test_me_returns_anonymous_admin_in_test_mode(self, client):
        """AUTH_BYPASS=True (test env) → /me returns anonymous admin."""
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert body["auth_enabled"] is False

    def test_logout_returns_ok(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_logout_clears_auth_cookie(self, client):
        r = client.post("/api/auth/logout")
        assert "auth_token" in r.headers.get("set-cookie", "")


# ── /api/auth/me/claim-codes ─────────────────────────────────────────────────

class TestClaimCodeRoutes:
    def test_create_claim_code_returns_code(self, client):
        r = client.post("/api/auth/me/claim-codes")
        assert r.status_code == 200
        body = r.json()
        assert "code" in body
        assert len(body["code"]) == 12
        assert body["code"] == body["code"].upper()
        assert body["used_at"] is None

    def test_list_claim_codes_includes_created(self, client):
        client.post("/api/auth/me/claim-codes")
        r = client.get("/api/auth/me/claim-codes")
        assert r.status_code == 200
        codes = r.json()
        assert isinstance(codes, list)
        assert len(codes) == 1

    def test_list_claim_codes_sorted_newest_first(self, client):
        client.post("/api/auth/me/claim-codes")
        client.post("/api/auth/me/claim-codes")
        r = client.get("/api/auth/me/claim-codes")
        codes = r.json()
        timestamps = [c.get("created_at", 0) for c in codes]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_revoke_claim_code(self, client):
        code = client.post("/api/auth/me/claim-codes").json()["code"]
        r = client.delete(f"/api/auth/me/claim-codes/{code}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_revoke_removes_code_from_list(self, client):
        code = client.post("/api/auth/me/claim-codes").json()["code"]
        client.delete(f"/api/auth/me/claim-codes/{code}")
        codes = client.get("/api/auth/me/claim-codes").json()
        assert not any(c["code"] == code for c in codes)

    def test_revoke_nonexistent_code_returns_404(self, client):
        r = client.delete("/api/auth/me/claim-codes/DOESNOTEXIST")
        assert r.status_code == 404

    def test_revoke_used_code_returns_404(self, client):
        from core.auth import consume_claim_code

        code = client.post("/api/auth/me/claim-codes").json()["code"]
        asyncio.run(consume_claim_code(code, "some-node"))
        r = client.delete(f"/api/auth/me/claim-codes/{code}")
        assert r.status_code == 404

    def test_claim_code_cap_returns_429(self, client):
        from core.auth import _MAX_ACTIVE_CLAIM_CODES_PER_USER

        for _ in range(_MAX_ACTIVE_CLAIM_CODES_PER_USER):
            client.post("/api/auth/me/claim-codes")
        r = client.post("/api/auth/me/claim-codes")
        assert r.status_code == 429


# ── /api/auth/me/nodes ────────────────────────────────────────────────────────

class TestMyNodes:
    def test_my_nodes_empty_when_no_ownership(self, client):
        r = client.get("/api/auth/me/nodes")
        assert r.status_code == 200
        assert r.json() == []

    def test_my_nodes_lists_owned_node(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        asyncio.run(set_node_owner("my-owned-node", ANONYMOUS_USER["id"]))
        try:
            r = client.get("/api/auth/me/nodes")
            assert r.status_code == 200
            node_ids = [n["node_id"] for n in r.json()]
            assert "my-owned-node" in node_ids
        finally:
            asyncio.run(set_node_owner("my-owned-node", None))

    def test_my_nodes_entry_has_expected_fields(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        asyncio.run(set_node_owner("field-check-node", ANONYMOUS_USER["id"]))
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == "field-check-node")
            for field in ("node_id", "name", "status", "is_synthetic"):
                assert field in node, f"Missing field: {field}"
        finally:
            asyncio.run(set_node_owner("field-check-node", None))


# ── OAuth state token (CSRF + open-redirect) ──────────────────────────────────

class TestOAuthStateToken:
    def test_valid_state_roundtrip(self):
        from routes.auth import _make_oauth_state, _verify_oauth_state

        state = _make_oauth_state("/dashboard")
        assert _verify_oauth_state(state) == "/dashboard"

    def test_tampered_state_rejected(self):
        from routes.auth import _make_oauth_state, _verify_oauth_state

        state = _make_oauth_state("/dashboard")
        tampered = state[:-4] + "XXXX"
        assert _verify_oauth_state(tampered) is None

    def test_invalid_format_state_rejected(self):
        from routes.auth import _verify_oauth_state

        assert _verify_oauth_state("notvalid") is None
        assert _verify_oauth_state("") is None
        assert _verify_oauth_state("a:b") is None

    def test_open_redirect_blocked_by_safe_redirect(self):
        from routes.auth import _safe_redirect

        assert _safe_redirect("//evil.com") == "/"
        assert _safe_redirect("https://evil.com/steal") == "/"
        assert _safe_redirect("/dashboard") == "/dashboard"

    def test_open_redirect_embedded_in_state_is_sanitized(self):
        """A state token carrying an open-redirect URL is accepted (HMAC valid)
        but the extracted redirect is sanitized to '/'."""
        from routes.auth import _make_oauth_state, _verify_oauth_state

        state = _make_oauth_state("//evil.com/steal")
        result = _verify_oauth_state(state)
        assert result == "/"


# ── /api/admin/invites ────────────────────────────────────────────────────────

class TestAdminInviteRoutes:
    def test_list_invites_empty(self, client):
        r = client.get("/api/admin/invites")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_invite(self, client):
        r = client.post("/api/admin/invites", json={"email": "alice@example.com", "role": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "alice@example.com"
        assert body["role"] == "user"
        assert body["used_at"] is None

    def test_create_invite_appears_in_list(self, client):
        client.post("/api/admin/invites", json={"email": "bob@example.com", "role": "user"})
        invites = client.get("/api/admin/invites").json()
        assert any(i["email"] == "bob@example.com" for i in invites)

    def test_create_invite_invalid_email_returns_400(self, client):
        r = client.post("/api/admin/invites", json={"email": "not-an-email", "role": "user"})
        assert r.status_code == 400

    def test_create_invite_invalid_role_returns_400(self, client):
        r = client.post("/api/admin/invites", json={"email": "c@example.com", "role": "owner"})
        assert r.status_code == 400

    def test_revoke_invite(self, client):
        token = client.post(
            "/api/admin/invites", json={"email": "d@example.com", "role": "user"}
        ).json()["token"]
        r = client.delete(f"/api/admin/invites/{token}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_revoke_invite_removes_from_list(self, client):
        token = client.post(
            "/api/admin/invites", json={"email": "e@example.com", "role": "user"}
        ).json()["token"]
        client.delete(f"/api/admin/invites/{token}")
        invites = client.get("/api/admin/invites").json()
        assert not any(i["token"] == token for i in invites)

    def test_revoke_nonexistent_invite_returns_404(self, client):
        r = client.delete("/api/admin/invites/does-not-exist")
        assert r.status_code == 404


# ── /api/admin/node-owners + /api/admin/nodes/{id}/owner ─────────────────────

class TestAdminNodeOwnerRoutes:
    def test_list_node_owners_empty(self, client):
        r = client.get("/api/admin/node-owners")
        assert r.status_code == 200
        assert r.json() == {}

    def test_list_node_owners_shows_owned(self, client):
        from core.auth import set_node_owner

        asyncio.run(set_node_owner("admin-test-node", "some-user-id"))
        try:
            owners = client.get("/api/admin/node-owners").json()
            assert "admin-test-node" in owners
            assert owners["admin-test-node"]["user_id"] == "some-user-id"
        finally:
            asyncio.run(set_node_owner("admin-test-node", None))

    def test_set_node_owner_null_clears_ownership(self, client):
        from core.auth import get_node_owner, set_node_owner

        asyncio.run(set_node_owner("clear-me-node", "some-user-id"))
        r = client.put("/api/admin/nodes/clear-me-node/owner", json={"user_id": None})
        assert r.status_code == 200
        assert r.json()["user_id"] is None
        assert asyncio.run(get_node_owner("clear-me-node")) is None

    def test_set_node_owner_invalid_uuid_returns_404(self, client):
        r = client.put("/api/admin/nodes/some-node/owner", json={"user_id": "not-a-uuid"})
        assert r.status_code == 404

    def test_set_node_owner_nonexistent_user_returns_404(self, client):
        import uuid
        r = client.put(
            "/api/admin/nodes/some-node/owner",
            json={"user_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404
