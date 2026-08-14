"""PUT /v1/nodes/config, through the app.

The version arithmetic itself is pinned in test_node_config_store.py. What is
tested here is what only the route can get wrong: the order of authentication and
validation, the wire shapes, the single commit, and whether a new version reaches
the pipeline.

Assertions read the database through a fresh query after the request rather than
off objects the handler left attached, per node_client's docstring: the handler
runs on its own loop and thread.
"""

import pytest
from sqlalchemy import select

from core import state
from core.nodes import Node, NodeConfig
from services import node_auth

# The geometry the registered_node fixture seeds as version 1, as a wire payload.
# Resending this must be a no-op, so test_the_baseline_matches_what_the_fixture_seeded
# pins it against the row rather than leaving it to drift into a silent change.
CONFIG = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

# What the fleet actually sends: neither antenna field characterised.
UNCHARACTERISED = dict(CONFIG, beam_width_deg=None, beam_azimuth_deg=None)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _versions(session, node_id: str) -> list[NodeConfig]:
    # The handler committed on this same session from another loop, so anything
    # already in the identity map predates the write.
    session.expire_all()
    rows = (
        (await session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id).order_by(NodeConfig.version)))
        .scalars()
        .all()
    )
    return list(rows)


async def test_the_baseline_matches_what_the_fixture_seeded(registered_node, node_session):
    """CONFIG is only a resend if it equals the seeded row field for field."""
    _token, node_id = registered_node

    row = (await _versions(node_session, node_id))[0]

    assert {field: getattr(row, field) for field in CONFIG} == CONFIG


# ── Versioning over the wire ─────────────────────────────────────────────────


async def test_an_unchanged_resend_returns_the_active_version(registered_node, node_client, node_session):
    token, node_id = registered_node

    first = node_client.put("/v1/nodes/config", headers=_auth(token), json=CONFIG)
    second = node_client.put("/v1/nodes/config", headers=_auth(token), json=CONFIG)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json() == {"config_version": 1}
    assert [(r.version, r.superseded_at) for r in await _versions(node_session, node_id)] == [(1, None)]


async def test_a_resend_with_both_antenna_fields_null_is_not_a_change(registered_node, node_client, node_session):
    """The one that bites. Every node in the fleet sends both as null, so a comparison
    made in SQL mints a version per resend for all of them, tells each one
    `config_stale` forever, and each one resends in answer.

    The first PUT moves the node to a configuration with both nulls, which is a real
    change against the fixture's seeded width; everything after it is the resend.

    See test_node_config_store.py for what this catches: hand-written or
    parameter-bound SQL, but not a SQLAlchemy `column == value`, which renders
    `IS NULL` and is accidentally correct.
    """
    token, node_id = registered_node

    minted = node_client.put("/v1/nodes/config", headers=_auth(token), json=UNCHARACTERISED)
    resends = [node_client.put("/v1/nodes/config", headers=_auth(token), json=UNCHARACTERISED) for _ in range(3)]

    assert [r.status_code for r in (minted, *resends)] == [200] * 4
    assert minted.json() == {"config_version": 2}
    assert [r.json() for r in resends] == [{"config_version": 2}] * 3
    rows = await _versions(node_session, node_id)
    assert [r.version for r in rows] == [1, 2]
    assert (rows[1].beam_width_deg, rows[1].beam_azimuth_deg) == (None, None)


async def test_a_changed_field_mints_the_next_version_and_supersedes_the_last(
    registered_node, node_client, node_session
):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    assert response.status_code == 200
    assert response.json() == {"config_version": 2}
    rows = await _versions(node_session, node_id)
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].superseded_at is not None
    assert rows[1].superseded_at is None
    assert rows[1].rx_lat == pytest.approx(51.50)


async def test_the_node_row_follows_the_active_version(registered_node, node_client, node_session):
    """`active_config_version` is what the heartbeat and detection paths compare a
    node's claimed version against, so a version minted and not recorded here would
    leave the node told `config_stale` immediately after it complied."""
    token, node_id = registered_node

    node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    node_session.expire_all()
    assert (await node_session.get(Node, node_id)).active_config_version == 2


async def test_the_route_commits(registered_node, node_client, node_session):
    """The service flushes and the route commits, so the new version survives a
    rollback of the session the request ran on."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    await node_session.rollback()

    assert [r.version for r in await _versions(node_session, node_id)] == [1, 2]


# ── Authentication comes first ───────────────────────────────────────────────


async def test_an_unauthenticated_call_is_401(registered_node, node_client, node_session):
    _token, node_id = registered_node

    response = node_client.put("/v1/nodes/config", json=CONFIG)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert len(await _versions(node_session, node_id)) == 1


async def test_a_revoked_token_is_401(registered_node, node_client, node_session):
    token, node_id = registered_node
    await node_auth.revoke_tokens(node_session, node_id, reason="test")
    await node_session.commit()

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=CONFIG)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_an_invalid_body_without_a_token_is_still_401(registered_node, node_client):
    """The ordering, and the reason this route reads its body by hand.

    A 400 reachable before the bearer resolves turns the difference between 400 and
    401 into an oracle: a caller could sort live tokens from dead ones by sending a
    body it knows to be invalid and reading which refusal comes back.
    """
    response = node_client.put("/v1/nodes/config", headers=_auth("not-a-real-token"), json={"rx_lat": "nonsense"})

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_an_unparseable_body_without_a_token_is_still_401(registered_node, node_client):
    response = node_client.put(
        "/v1/nodes/config",
        headers={"Authorization": "Bearer not-a-real-token", "Content-Type": "application/json"},
        content=b"{not json",
    )

    assert response.status_code == 401


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (dict(CONFIG, fs_hz=1), "fs_hz"),
        (dict(CONFIG, rx_lat=91.0), "rx_lat"),
        (dict(CONFIG, cpi_s=0), "cpi_s"),
        (dict(CONFIG, beam_azimuth_deg=360.0), "beam_azimuth_deg"),
        (dict(CONFIG, beam_width_deg=0), "beam_width_deg"),
        (dict(CONFIG, tx_callsign=""), "tx_callsign"),
        ({k: v for k, v in CONFIG.items() if k != "cpi_s"}, "cpi_s"),
        (dict(CONFIG, orbit_km=7), "orbit_km"),
    ],
    ids=[
        "below_bound",
        "above_bound",
        "exclusive_min",
        "azimuth_exclusive_max",
        "width_exclusive_min",
        "empty_callsign",
        "missing",
        "unknown",
    ],
)
async def test_an_invalid_config_is_400_naming_the_field(payload, field, registered_node, node_client, node_session):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=payload)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_config", "detail": field}
    assert len(await _versions(node_session, node_id)) == 1


async def test_a_long_unknown_field_is_400_rather_than_500(registered_node, node_client, node_session):
    """The detail on an unknown field is a key the caller chose, and `Error.detail` is
    bounded at 512 characters. Passed through whole, a longer key fails ErrorBody's own
    validation inside the handler and the node gets a 500, which it retries, for a body
    that will never be accepted. A 600-byte key sits well inside the 8 KiB path cap.
    """
    token, node_id = registered_node
    key = "z" * 600

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, **{key: 1}))

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_config"
    assert body["detail"] == key[:512]
    assert len(await _versions(node_session, node_id)) == 1


async def test_a_body_that_is_not_an_object_is_400(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=[CONFIG])

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_config", "detail": "config"}


async def test_a_body_that_is_not_json_is_400(registered_node, node_client):
    """Rejected as a configuration rather than raised out of the handler: for the
    node the remedy is the same, and a 500 would be retried."""
    token, _node_id = registered_node

    response = node_client.put(
        "/v1/nodes/config",
        headers={**_auth(token), "Content-Type": "application/json"},
        content=b"{not json",
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_config", "detail": "config"}


# ── The pipeline ─────────────────────────────────────────────────────────────


async def test_a_new_version_reaches_the_pipeline(registered_node, node_client):
    """Without this the solver goes on using the geometry the node has just told us
    it no longer has, until the next restart primes it from the table."""
    token, node_id = registered_node
    before = state.connected_nodes[node_id]["config_hash"]

    node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    entry = state.connected_nodes[node_id]
    assert entry["config"]["rx_lat"] == pytest.approx(51.50)
    assert entry["config_hash"] != before
    assert state.node_associator.node_geometries[node_id].rx_lat == pytest.approx(51.50)


async def test_an_unchanged_resend_does_not_touch_the_pipeline(registered_node, node_client, monkeypatch):
    """Re-registering on every resend would rebuild each node's analytics and
    associator state 30 times a minute for a configuration that has not moved."""
    import routes.node_config as route

    calls = []

    async def _record(_session, node):
        calls.append(node.node_id)

    monkeypatch.setattr(route, "register_with_pipeline", _record)
    token, _node_id = registered_node

    node_client.put("/v1/nodes/config", headers=_auth(token), json=CONFIG)

    assert calls == []


async def test_a_change_reaches_the_pipeline_once(registered_node, node_client, monkeypatch):
    import routes.node_config as route

    calls = []

    async def _record(_session, node):
        calls.append(node.node_id)

    monkeypatch.setattr(route, "register_with_pipeline", _record)
    token, node_id = registered_node

    node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    assert calls == [node_id]


async def test_a_failed_handoff_alerts_and_keeps_the_stored_version(
    registered_node, node_client, node_session, monkeypatch, alerts
):
    """A handoff that will not go is an alert, not a 500.

    The version is committed before the pipeline is told, so failing the request
    would have the node resend a configuration the server already holds, and the
    resend would be a no-op returning the same version. Not hypothetical:
    retina-analytics subtracts a null `beam_width_deg` when re-registering a node,
    and a null width is what the whole fleet sends.
    """
    import routes.node_config as route

    async def _explode(_session, _node):
        raise TypeError("unsupported operand type(s) for -: 'float' and 'NoneType'")

    monkeypatch.setattr(route, "register_with_pipeline", _explode)
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(CONFIG, rx_lat=51.50))

    assert response.status_code == 200
    assert response.json() == {"config_version": 2}
    assert [r.version for r in await _versions(node_session, node_id)] == [1, 2]
    assert [a[0] for a in alerts] == ["node_config_handoff_failed"]
    assert alerts[0][2]["config_version"] == 2
