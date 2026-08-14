"""The v1 node fixtures in conftest.py are infrastructure, so they get tests.

Four endpoint suites are written in parallel against those signatures, so a
fixture that quietly stops doing what it says would fail three other suites in
ways that point nowhere near the cause. Each test below pins one clause of the
contract: the arity, the ordering, and the state the fixture claims to leave
behind.
"""

import pytest
from sqlalchemy import select
from starlette.requests import Request

from core import state
from core.nodes import Node, NodeConfig, NodeToken
from core.users import User, get_async_session
from services import mender, node_auth


def _bearer_request(token: str) -> Request:
    """The minimum scope bearer_node reads: the Authorization header."""
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


# ── registered_node ──────────────────────────────────────────────────────────


async def test_registered_node_unpacks_as_token_then_node_id(registered_node):
    """The ordering is fixed by the suites already written against it, so it is
    worth a test of its own: both members are strings, and swapping them would
    otherwise only surface as a puzzling 401."""
    token, node_id = registered_node

    assert node_id == "ret1a2b3c4d"
    assert token != node_id
    # RegisterResponse.token is min_length=32, so a real token clears this too.
    assert len(token) >= 32


async def test_the_seeded_token_is_a_live_row_hashing_to_the_stored_hash(registered_node, node_session):
    token, node_id = registered_node

    rows = (await node_session.execute(select(NodeToken).where(NodeToken.node_id == node_id))).scalars().all()

    assert len(rows) == 1
    assert rows[0].revoked_at is None
    assert rows[0].token_hash == node_auth.token_hash(token)


async def test_the_seeded_token_actually_authenticates(registered_node, node_session):
    """The hash comparison above is this end's arithmetic on both sides, so it
    would still pass if bearer_node's own predicate changed. This drives the
    real dependency instead."""
    token, node_id = registered_node

    assert await node_auth.bearer_node(_bearer_request(token), node_session) == node_id


async def test_the_seeded_node_is_active_at_config_version_one(registered_node, node_session):
    _token, node_id = registered_node

    node = await node_session.get(Node, node_id)
    assert node.status == "active"
    assert node.active_config_version == 1


async def test_the_seeded_node_has_one_active_configuration(registered_node, node_session):
    _token, node_id = registered_node

    configs = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id))).scalars().all()

    assert len(configs) == 1
    assert configs[0].version == 1
    assert configs[0].superseded_at is None
    # From validate_config's output, so a config the real validator would reject
    # cannot reach the row.
    assert configs[0].rx_lat == pytest.approx(51.42)
    # Null rather than 0.0: broadside, not aimed due north.
    assert configs[0].beam_azimuth_deg is None


async def test_the_seeded_node_is_registered_with_the_pipeline(registered_node):
    """Without this the heartbeat handler raises KeyError on connected_nodes."""
    _token, node_id = registered_node

    entry = state.connected_nodes[node_id]
    assert entry["status"] == "active"
    assert entry["config"]["rx_lat"] == pytest.approx(51.42)
    assert state.node_associator.node_geometries[node_id].rx_lat == pytest.approx(51.42)


async def test_the_seed_survives_a_rollback(registered_node, node_session):
    """Committed rather than flushed, so a handler that rolls back its own
    transaction cannot take the fixture's node with it."""
    _token, node_id = registered_node

    await node_session.rollback()

    assert await node_session.get(Node, node_id) is not None


# ── The Mender fixtures ──────────────────────────────────────────────────────


async def test_accepted_in_mender_resolves_to_an_accepted_record(accepted_in_mender):
    accepted_in_mender("ret1a2b3c4d")

    record = await mender.lookup_device("ret1a2b3c4d")

    assert record is not None
    assert record.auth_status == "accepted"
    # Registration signing needs the key itself, so an accepted record with an
    # empty pubkey would not stand in for one.
    assert record.auth_set_pubkey


async def test_pending_in_mender_resolves_but_is_not_accepted(pending_in_mender):
    pending_in_mender("retpending1")

    record = await mender.lookup_device("retpending1")

    assert record is not None
    assert record.auth_status == "pending"


async def test_unknown_in_mender_resolves_to_none(unknown_in_mender):
    unknown_in_mender("retdeadbeef")

    assert await mender.lookup_device("retdeadbeef") is None


async def test_mender_down_raises_mender_unreachable(mender_down):
    mender_down()

    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_each_mender_fixture_is_undone_at_teardown():
    """monkeypatch restores _transport, so a suite mixing the four fixtures does
    not inherit the previous test's answer."""
    assert mender._transport is None


# ── alerts ───────────────────────────────────────────────────────────────────


async def test_alerts_captures_a_call_time_import(alerts):
    """The import sits inside the function deliberately: that is the only shape
    the patch can intercept, and it is the shape services/node_pipeline.py uses."""

    def _handler_style_call():
        from services.alerting import send_alert

        send_alert("mender_unreachable", "Mender could not be asked", {"node_id": "ret1a2b3c4d"})

    _handler_style_call()

    assert any(a[0] == "mender_unreachable" for a in alerts)
    alert_type, message, meta = alerts[0]
    assert (alert_type, message) == ("mender_unreachable", "Mender could not be asked")
    assert meta == {"node_id": "ret1a2b3c4d"}


async def test_alerts_is_empty_when_nothing_alerts(alerts):
    assert alerts == []


# ── node_client ──────────────────────────────────────────────────────────────


async def test_node_client_requests_read_the_node_session_database(node_client, node_session):
    """The end-to-end proof of the get_async_session override, driven through a
    handler that already takes that dependency.

    /api/admin/users is not a node route, but it is the only endpoint on the app
    today that reads the session; the node routes it exists for have no handlers
    yet. The row is seeded on node_session and never on the main database, so a
    request that sees it can only have resolved the dependency to this session.
    """
    node_session.add(User(email="fixture@example.com", hashed_password="unused"))
    await node_session.commit()

    response = node_client.get("/api/admin/users")

    assert response.status_code == 200
    assert [u["email"] for u in response.json()] == ["fixture@example.com"]


async def test_node_client_reports_status_json_content_and_headers(node_client):
    """The four attributes the suites read off a response."""
    response = node_client.get("/api/admin/users")

    assert response.status_code == 200
    assert response.json() == []
    assert response.content == b"[]"
    # Absent here, but the 429 tests read it the same way, so the accessor has
    # to answer None rather than raise on a response that carries no header.
    assert response.headers.get("retry-after") is None


async def test_node_client_does_not_run_the_lifespan(node_client):
    """The bare TestClient is what keeps the frame processor workers from
    starting; with the lifespan running they would drain state.frame_queue
    underneath any detection test asserting on what a handler queued."""
    node_client.get("/api/admin/users")

    assert node_client.portal is None


@pytest.fixture
def _no_override_after_teardown():
    """Assert the app is left clean, after node_client has been torn down.

    Requested ahead of node_client in the test signature below, so it is set up
    first and finalised last; a finaliser on anything depending on node_client
    would run before node_client's own and see the override still installed.
    """
    yield
    import main

    assert get_async_session not in main.app.dependency_overrides


async def test_node_client_installs_and_removes_the_override(_no_override_after_teardown, node_client, node_session):
    import main

    override = main.app.dependency_overrides[get_async_session]

    assert [session async for session in override()] == [node_session]
