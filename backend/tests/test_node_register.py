"""POST /v1/nodes/register.

Three things are asserted here that are requirements rather than preferences,
and each has a test whose name says so: the rate limiter runs before the Mender
call, configuration validation runs after identity resolution, and every refusal
class answers with one body and one jittered Retry-After.

The re-registration tests stand in for the reflash path: a board that comes back
with the same node_id keeps its node_ref and its configuration version, and its
previous token dies. There is no heartbeat handler on this branch to spend the
dead token against, so the revocation is asserted through `bearer_node`, which
is the predicate every authenticated endpoint resolves through anyway.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from core import state
from core.nodes import Node, NodeConfig, NodeToken
from routes import node_register
from services import alerting, node_auth
from services.node_rate_limits import REGISTRATION_LIMITS, registration_limiter

NODE_ID = "ret1a2b3c4d"
UNKNOWN_ID = "retdeadbeef"
PENDING_ID = "ret0badcafe"
UNREACHABLE_ID = "retc0ffee00"
OTHER_ID = "ret000000ff"

# The fifteen fields `NodeConfig` requires, at contract 1.1.1. Same geometry as
# conftest's `_NODE_CONFIG_VALUES`, restated rather than imported: these are the
# values a node puts on the wire, and the fixture's are the columns a seeded row
# carries, so a test that swapped one for the other would stop noticing if the
# handler dropped a field between them.
VALID_CONFIG = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    # Free text with spaces, per the contract: not a regulatory callsign.
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    # Null rather than absent: broadside, not aimed due north.
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

AGREEMENTS = {
    "licence": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "publication": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z", "choice": "public"},
}

HOURLY_LIMIT = REGISTRATION_LIMITS[0][0]


def _body(node_id: str = NODE_ID, **overrides) -> dict:
    body = {
        "node_id": node_id,
        "board_model": "raspberrypi5-4gb",
        "agreements": AGREEMENTS,
        "config": dict(VALID_CONFIG),
    }
    return body | overrides


def _register(client, node_id: str = NODE_ID, **overrides):
    return client.post("/v1/nodes/register", json=_body(node_id, **overrides))


def _bearer_request(token: str) -> Request:
    """The minimum scope `bearer_node` reads, as in tests/test_node_fixtures.py."""
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


def _committed(session, sql: str) -> list[tuple]:
    """Read the fixture database through a connection of its own.

    Everything else here asserts through `node_session`, which is the session the
    handler wrote on, so a flush looks exactly like a commit: the writes are
    visible either way. A second connection sees only what has been committed,
    which is the difference the transaction contract is about. A handler that
    flushed rather than committed would hand a node a token that the request's
    session rolls back on close, and the node would 401 forever afterwards
    holding a credential the server never stored.
    """
    # closing() as well as the connection's own context manager: that one ends
    # the transaction and leaves the handle open.
    with closing(sqlite3.connect(session.get_bind().url.database)) as connection:
        return connection.execute(sql).fetchall()


async def _resolves(session, token: str) -> bool:
    try:
        await node_auth.bearer_node(_bearer_request(token), session)
    except HTTPException as exc:
        assert exc.status_code == 401
        return False
    return True


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """The limiter counters and the pipeline registry both outlive a test.

    Both are module-level singletons rather than per-request state, so a
    registration in one test would otherwise spend another's allowance and leave
    a node in `state.connected_nodes` for a suite that never registered one.
    """
    registration_limiter.reset()
    yield
    registration_limiter.reset()
    with state.connected_nodes_lock:
        for node_id in (NODE_ID, UNKNOWN_ID, PENDING_ID, UNREACHABLE_ID, OTHER_ID):
            state.connected_nodes.pop(node_id, None)


# ── The happy path ───────────────────────────────────────────────────────────


def test_an_accepted_device_registers(node_client, accepted_in_mender):
    accepted_in_mender(NODE_ID)

    response = _register(node_client)

    assert response.status_code == 200
    payload = response.json()
    assert 32 <= len(payload["token"]) <= 128
    assert payload["node_ref"].startswith("nde") and len(payload["node_ref"]) == 15
    assert payload["config_version"] == 1
    # RFC 3339 UTC with a Z, which is the form the node parses to measure its
    # clock offset.
    assert payload["server_time"].endswith("Z")


async def test_the_registered_node_is_active_and_holds_a_live_token(node_client, accepted_in_mender, node_session):
    accepted_in_mender(NODE_ID)

    token = _register(node_client).json()["token"]

    node = await node_session.get(Node, NODE_ID)
    assert node.status == "active"
    assert node.board_model == "raspberrypi5-4gb"
    assert node.active_config_version == 1
    assert await _resolves(node_session, token)


async def test_the_configuration_is_stored_as_version_one(node_client, accepted_in_mender, node_session):
    accepted_in_mender(NODE_ID)

    _register(node_client)

    rows = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == NODE_ID))).scalars().all()
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].superseded_at is None
    assert rows[0].rx_lat == pytest.approx(51.42)
    assert rows[0].cpi_s == pytest.approx(0.5)
    # Null survives the round trip: 0.0 would aim an unaimed node due north.
    assert rows[0].beam_azimuth_deg is None


def test_the_registration_is_committed_rather_than_left_pending(node_client, accepted_in_mender, node_session):
    """The five writes are one transaction, and it is this handler that ends it."""
    accepted_in_mender(NODE_ID)

    _register(node_client)

    assert _committed(node_session, "SELECT node_id FROM nodes") == [(NODE_ID,)]
    assert _committed(node_session, "SELECT count(*) FROM node_tokens WHERE revoked_at IS NULL") == [(1,)]
    assert _committed(node_session, "SELECT version FROM node_configs") == [(1,)]


async def test_the_agreement_records_are_stored(node_client, accepted_in_mender, node_session):
    """What the owner accepted is the record the licence rests on, so it is
    asserted field by field rather than as a spot check."""
    accepted_in_mender(NODE_ID)

    _register(node_client)

    node = await node_session.get(Node, NODE_ID)
    assert node.licence_version == "2026-07-01"
    assert node.licence_accepted_at is not None
    assert node.remote_management_version == "2026-07-01"
    assert node.remote_management_accepted_at is not None
    assert node.publication == "public"
    assert node.publication_version == "2026-07-01"
    assert node.publication_chosen_at is not None


async def test_an_acceptance_sent_with_an_offset_is_stored_as_the_instant(
    node_client, accepted_in_mender, node_session
):
    """SQLite stores no offset, so an aware value is written as its own wall
    clock and read back as the UTC everything else in these tables is. A licence
    accepted at 09:12+02:00 filed as 09:12 UTC is two hours late, and can end up
    appearing to postdate the registration it authorised."""
    accepted_in_mender(NODE_ID)
    offset = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00+02:00"}

    _register(node_client, agreements=AGREEMENTS | {"licence": offset})

    node = await node_session.get(Node, NODE_ID)
    stored = node.licence_accepted_at.replace(tzinfo=UTC)
    assert stored == datetime(2026, 7, 31, 7, 12, tzinfo=UTC)


async def test_a_private_choice_is_recorded_as_private(node_client, accepted_in_mender, node_session):
    accepted_in_mender(NODE_ID)
    agreements = AGREEMENTS | {"publication": AGREEMENTS["publication"] | {"choice": "private"}}

    _register(node_client, agreements=agreements)

    node = await node_session.get(Node, NODE_ID)
    assert node.publication == "private"


def test_agreements_missing_publication_is_rejected(node_client, accepted_in_mender):
    """A refusal from the model, not a server-side default.

    The ADR's "a registration with no recorded choice resolves to public" is
    about a node with no stored choice, not about a body that omits the field,
    which `Agreements` declares required.
    """
    accepted_in_mender(NODE_ID)
    agreements = {k: v for k, v in AGREEMENTS.items() if k != "publication"}

    response = _register(node_client, agreements=agreements)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_body", "detail": "agreements.publication"}


async def test_the_node_reaches_the_pipeline(node_client, accepted_in_mender, node_session):
    """Without this the node is registered and invisible: nothing else puts a
    node into `connected_nodes` between deploys."""
    accepted_in_mender(NODE_ID)

    _register(node_client)

    entry = state.connected_nodes[NODE_ID]
    assert entry["status"] == "active"
    assert entry["config"]["rx_lat"] == pytest.approx(51.42)
    assert state.node_associator.node_geometries[NODE_ID].rx_lat == pytest.approx(51.42)


# ── Refusals ─────────────────────────────────────────────────────────────────


def test_every_refusal_class_shares_one_body(
    node_client, accepted_in_mender, unknown_in_mender, pending_in_mender, mender_down
):
    """Unknown, not yet accepted, unreachable and rate limited are one answer.

    Byte-identical bodies rather than equal JSON: a difference in key order or
    whitespace is as much of an oracle as a difference in wording.
    """
    unknown_in_mender(UNKNOWN_ID)
    unknown = _register(node_client, UNKNOWN_ID)
    pending_in_mender(PENDING_ID)
    pending = _register(node_client, PENDING_ID)
    mender_down()
    unreachable = _register(node_client, UNREACHABLE_ID)

    accepted_in_mender(NODE_ID)
    for _ in range(HOURLY_LIMIT):
        assert _register(node_client).status_code == 200
    limited = _register(node_client)

    refusals = [unknown, pending, unreachable, limited]
    assert {r.status_code for r in refusals} == {403}
    assert len({r.content for r in refusals}) == 1
    assert all(int(r.headers["retry-after"]) > 0 for r in refusals)


def test_a_colliding_registration_is_refused_rather_than_a_500(
    node_client, accepted_in_mender, node_session, monkeypatch
):
    """Two registrations for one node_id can interleave at the Mender lookup,
    which is an await on a network call: both read no row, both insert, and the
    loser hits the primary key. The node then sees a defined refusal it already
    knows how to back off from, rather than a 500, and its retry finds the row
    the winner committed.

    Driven by making the write raise rather than by racing two requests: the
    fixture client is synchronous, and what wants asserting is the handler's
    answer to the collision rather than the scheduler's ability to produce one.
    """
    accepted_in_mender(NODE_ID)

    async def _collide(*_args, **_kwargs):
        raise IntegrityError("INSERT INTO nodes", {}, Exception("UNIQUE constraint failed: nodes.node_id"))

    monkeypatch.setattr(node_register, "upsert_config", _collide)

    response = _register(node_client)

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert int(response.headers["retry-after"]) > 0
    # Rolled back, so the winner's row is the only one that can exist.
    assert _committed(node_session, "SELECT count(*) FROM nodes") == [(0,)]


async def test_a_refused_registration_writes_nothing(node_client, unknown_in_mender, node_session):
    unknown_in_mender(UNKNOWN_ID)

    _register(node_client, UNKNOWN_ID)

    assert await node_session.get(Node, UNKNOWN_ID) is None
    assert (await node_session.execute(select(NodeToken))).scalars().all() == []


def test_mender_unreachable_raises_the_alert(node_client, mender_down, alerts):
    """The one alert distinguishing "could not ask" from "does not know". They
    are the same 403 on the wire, but only one means the whole fleet is stuck."""
    mender_down()

    assert _register(node_client, UNREACHABLE_ID).status_code == 403
    assert [a[0] for a in alerts] == ["mender_unreachable"]


def test_an_unknown_device_raises_no_alert(node_client, unknown_in_mender, alerts):
    """Mender answering "no" is the ordinary case during bring-up. Alerting on
    it would bury the unreachable alert that matters."""
    unknown_in_mender(UNKNOWN_ID)

    assert _register(node_client, UNKNOWN_ID).status_code == 403
    assert alerts == []


# ── The two orderings ────────────────────────────────────────────────────────


def test_an_invalid_config_is_a_400_only_after_identity_resolves(node_client, accepted_in_mender, unknown_in_mender):
    """A 400 reachable before identity resolution would turn the difference
    between 400 and 403 into an oracle for which identities exist."""
    accepted_in_mender(NODE_ID)

    bad = _register(node_client, config=dict(VALID_CONFIG, rx_lat=999))

    assert bad.status_code == 400
    # `detail` is the field name rather than a sentence: retina-gui puts the
    # message next to the input, and this end does not own its wording.
    assert bad.json() == {"error": "invalid_config", "detail": "rx_lat"}

    unknown_in_mender(UNKNOWN_ID)
    both_wrong = _register(node_client, UNKNOWN_ID, config=dict(VALID_CONFIG, rx_lat=999))
    assert both_wrong.status_code == 403


def test_an_over_long_unknown_config_key_is_still_a_400(node_client, accepted_in_mender):
    """The rejected field is named back to the caller, and an unknown key is
    whatever JSON the caller sent. A key longer than the contract's bound on
    `detail` is well inside the 8 KiB body cap, so it has to be truncated rather
    than trusted to fit: unbounded, it fails the response model's own validation
    and turns this 400 into a 500 on an unauthenticated endpoint."""
    accepted_in_mender(NODE_ID)

    response = _register(node_client, config=dict(VALID_CONFIG, **{"x" * 513: 1}))

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_config"
    assert body["detail"] == "x" * 512


def test_the_rate_limiter_fires_before_the_mender_call(node_client, accepted_in_mender, mender_down, alerts):
    """D26. An unauthenticated caller must not be able to drive traffic at the
    Mender tenant by looping on registration.

    Mender is taken down once the allowance is spent, so a handler that asked it
    anyway would raise `mender_unreachable` and fail here rather than silently
    answering the same 403 either way.
    """
    accepted_in_mender(NODE_ID)
    for _ in range(HOURLY_LIMIT):
        assert _register(node_client).status_code == 200

    mender_down()
    limited = _register(node_client)

    assert limited.status_code == 403
    # Not `alerts == []`: the four re-registrations above each raise a hold.
    assert "mender_unreachable" not in [a[0] for a in alerts]


def test_the_limiter_is_keyed_per_node_id(node_client, accepted_in_mender):
    """One board in a retry loop must not lock the rest of the fleet out."""
    accepted_in_mender(NODE_ID)
    for _ in range(HOURLY_LIMIT):
        assert _register(node_client).status_code == 200
    assert _register(node_client).status_code == 403

    accepted_in_mender(OTHER_ID)
    assert _register(node_client, OTHER_ID).status_code == 200


def test_a_malformed_node_id_never_reaches_mender(node_client, mender_down, alerts):
    """`lookup_device` matches on `identity_data["node_id"]`, which Mender's
    records default to the empty string, so an empty identity would resolve to
    whichever device carries none. The model's pattern is what stops it, so a
    body that fails it must be refused before the lookup rather than by it."""
    mender_down()

    assert node_client.post("/v1/nodes/register", json=_body() | {"node_id": ""}).status_code == 400
    assert node_client.post("/v1/nodes/register", json=_body() | {"node_id": "retZZZZZZZZ"}).status_code == 400
    assert alerts == []


# ── Re-registration, the reflash path ────────────────────────────────────────


async def test_re_registration_revokes_the_previous_token(node_client, accepted_in_mender, node_session, alerts):
    accepted_in_mender(NODE_ID)
    first = _register(node_client).json()["token"]

    second = _register(node_client).json()["token"]

    assert second != first
    assert not await _resolves(node_session, first)
    assert await _resolves(node_session, second)
    revoked = (
        (await node_session.execute(select(NodeToken).where(NodeToken.token_hash == node_auth.token_hash(first))))
        .scalars()
        .one()
    )
    assert revoked.revoked_reason == "reflash"
    # The alert is the whole of what makes a takeover visible, since this build
    # cannot gate re-registration on an operator reactivation it has no way to
    # trigger.
    assert [a[0] for a in alerts] == ["registration_held"]


def test_the_hold_alert_fires_after_the_commit(node_client, accepted_in_mender, node_session, monkeypatch):
    """A rolled back registration must not alert about a revocation that did not
    happen, so the alert is asserted on what was committed when it fired rather
    than on the order of two lines.

    Patched here rather than through the `alerts` fixture, which captures the
    call but not the state at the moment of it.
    """
    committed_at_alert = []

    def _capture(alert_type: str, _message: str, _meta: dict | None = None) -> None:
        revoked = _committed(node_session, "SELECT count(*) FROM node_tokens WHERE revoked_reason = 'reflash'")
        committed_at_alert.append((alert_type, revoked[0][0]))

    monkeypatch.setattr(alerting, "send_alert", _capture)
    accepted_in_mender(NODE_ID)
    _register(node_client)

    _register(node_client)

    assert committed_at_alert == [("registration_held", 1)]


def test_a_first_registration_raises_no_hold(node_client, accepted_in_mender, alerts):
    accepted_in_mender(NODE_ID)

    _register(node_client)

    assert alerts == []


def test_re_registration_keeps_the_node_ref_and_returns_the_held_version(node_client, accepted_in_mender):
    """A board told its version is 1 when the server holds 2 gets a 409 on every
    frame afterwards, forever."""
    accepted_in_mender(NODE_ID)
    first = _register(node_client).json()
    moved = _register(node_client, config=dict(VALID_CONFIG, max_range_km=60.0)).json()

    again = _register(node_client, config=dict(VALID_CONFIG, max_range_km=60.0)).json()

    assert moved["config_version"] == 2
    assert again["config_version"] == 2
    assert first["node_ref"] == moved["node_ref"] == again["node_ref"]


async def test_a_blocked_board_re_registers_without_returning_to_the_pipeline(
    node_client, accepted_in_mender, node_session
):
    """A reflash must not be the way to undo an operator's block.

    The block is honoured by the detection handler, which reads node.status, and
    by the pipeline entry, which is what puts a node on the map. Registration
    writes the second of those, so it has to respect the first.
    """
    accepted_in_mender(NODE_ID)
    _register(node_client)
    node = await node_session.get(Node, NODE_ID)
    node.status = "blocked"
    await node_session.commit()
    with state.connected_nodes_lock:
        state.connected_nodes.pop(NODE_ID, None)

    assert _register(node_client).status_code == 200

    assert NODE_ID not in state.connected_nodes
    assert (await node_session.get(Node, NODE_ID)).status == "blocked"


async def test_re_registration_records_a_withdrawn_publication_choice(node_client, accepted_in_mender, node_session):
    """The agreement set is rewritten on every registration, so a board that
    comes back with a changed choice is not left carrying the old one."""
    accepted_in_mender(NODE_ID)
    _register(node_client)
    agreements = AGREEMENTS | {"publication": AGREEMENTS["publication"] | {"choice": "private"}}

    _register(node_client, agreements=agreements)

    node = await node_session.get(Node, NODE_ID)
    assert node.publication == "private"
