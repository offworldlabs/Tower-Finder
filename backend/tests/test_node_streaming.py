"""POST /v1/nodes/detection and POST /v1/nodes/heartbeat.

The two hot-path handlers, tested through the app rather than by calling them:
the bearer dependency, the per-token limiter and the body caps are all part of
what these endpoints are, and none of them is exercised by a direct call.

Bodies here are transcribed from the wire contract's examples rather than
minimised, so a bound tightening fails a test instead of passing one that never
sent a realistic frame.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from core import state
from core.nodes import Node, NodeConfig, NodeToken
from services.node_rate_limits import token_rate_limiter

DETECTION = "/v1/nodes/detection"
HEARTBEAT = "/v1/nodes/heartbeat"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """A fresh allowance per test.

    The limiter is a module-level singleton, so counters otherwise carry across
    tests and a suite run in one order refuses requests a suite run in another
    admits. conftest's `_reset_module_state` does not cover it: that fixture's
    module list is shared with three other branches in flight, and this one
    store is cheaper to reset here than to contend over there.
    """
    token_rate_limiter.reset()
    yield
    token_rate_limiter.reset()


@pytest.fixture
def frozen_limiter(monkeypatch):
    """The route's limiter, on a clock that never advances.

    The allowance is a fixed window aligned on `time.monotonic`, so a test that
    spends it against the real clock is really asserting that its own requests
    all fall inside one window. They usually do and occasionally do not: a
    boundary falling mid-test hands the burst a fresh allowance, and the request
    that should have been refused is admitted. Frozen, the window never rolls,
    so the nth request is refused because the limit is n-1 and for no other
    reason.

    Patched on `routes.node_stream` rather than on the service module, because
    the route binds the singleton by name at import;
    `test_the_route_binds_the_shared_limiter` is what keeps that indirection
    honest.
    """
    from routes import node_stream
    from services.node_rate_limits import TokenRateLimiter

    limiter = TokenRateLimiter(clock=lambda: 1_000.0)
    monkeypatch.setattr(node_stream, "token_rate_limiter", limiter)
    return limiter


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _frame(**overrides) -> dict:
    """One frame, from the contract's own examples. Two parallel detections."""
    frame = {
        "t": 1753900000.123,
        "seq": 918273,
        "boot_id": "k3n8v2qp71ab",
        "config_version": 1,
        "delay": [12.4, 30.1],
        "doppler": [-118.0, 44.5],
        "snr": [14.2, 9.8],
        "adsb_hex": ["4ca1f2", None],
    }
    frame.update(overrides)
    return frame


def _beat(**overrides) -> dict:
    """One heartbeat. `boot_id` is required since contract 1.1.0."""
    beat = {"state": "streaming", "uptime_s": 9, "boot_id": "k3n8v2qp71ab", "config_version": 1}
    beat.update(overrides)
    return beat


def _queued() -> list[tuple[str, dict]]:
    """Everything on the frame queue, drained."""
    frames = []
    while not state.frame_queue.empty():
        frames.append(state.frame_queue.get_nowait())
    return frames


async def _set_status(session, node_id: str, status: str) -> None:
    node = await session.get(Node, node_id)
    node.status = status
    await session.commit()


async def _supersede_config(session, node_id: str) -> None:
    """Move the node to version 2, leaving version 1 superseded but readable."""
    version_one = (
        (await session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id, NodeConfig.version == 1)))
        .scalars()
        .one()
    )
    columns = {
        column.name: getattr(version_one, column.name)
        for column in NodeConfig.__table__.columns
        if column.name not in ("id", "version", "created_at", "superseded_at")
    }
    version_one.superseded_at = datetime.now(UTC)
    session.add(NodeConfig(version=2, **columns))
    node = await session.get(Node, node_id)
    node.active_config_version = 2
    await session.commit()


# ── detection ────────────────────────────────────────────────────────────────


async def test_a_frame_is_accepted_whole_and_reaches_the_queue(registered_node, node_client):
    token, node_id = registered_node

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.status_code == 202
    assert response.json() == {"accepted": 2, "config_stale": False, "streaming_allowed": True}
    ((queued_id, frame),) = _queued()
    assert queued_id == node_id
    assert frame["delay"] == [12.4, 30.1]
    assert frame["doppler"] == [-118.0, 44.5]
    assert frame["snr"] == [14.2, 9.8]
    assert frame["adsb_hex"] == ["4ca1f2", None]


async def test_the_queued_frame_is_attributed_from_the_token(registered_node, node_client):
    """The body names no node, so `_node_id` can only have come from the bearer.

    `submit_frame` stamps it, and the pipeline's readers prefer it over the
    tuple's key, so a frame reaching them unstamped would be filed under
    whatever the reader had to hand.
    """
    token, node_id = registered_node

    node_client.post(DETECTION, headers=_auth(token), json=_frame())

    ((_, frame),) = _queued()
    assert frame["_node_id"] == node_id


async def test_the_capture_time_is_converted_to_milliseconds(registered_node, node_client):
    """`t` is epoch seconds on the wire and the queue's readers expect ms."""
    token, _ = registered_node

    node_client.post(DETECTION, headers=_auth(token), json=_frame(t=1753900000.123))

    ((_, frame),) = _queued()
    assert frame["timestamp"] == 1753900000123


async def test_the_restart_local_counter_travels_with_the_frame(registered_node, node_client):
    """`seq` and `boot_id` are what loss is counted against, and only together."""
    token, _ = registered_node

    node_client.post(DETECTION, headers=_auth(token), json=_frame(seq=41, boot_id="k3n8v2qp71ab"))

    ((_, frame),) = _queued()
    assert (frame["seq"], frame["boot_id"]) == (41, "k3n8v2qp71ab")


async def test_the_association_is_not_filed_as_a_position(registered_node, node_client):
    """`adsb_hex` is an association and carries no lat/lon.

    frame_processor reads `adsb` as position reports, so the hexes travel under
    their own key: filing them there would file an empty position per detection.
    """
    token, _ = registered_node

    node_client.post(DETECTION, headers=_auth(token), json=_frame())

    ((_, frame),) = _queued()
    assert "adsb" not in frame


async def test_an_empty_frame_is_valid_and_still_reaches_the_queue(registered_node, node_client):
    """The distinction an empty frame carries is between a node with nothing to
    report and a node that has stopped, and it only survives if the frame is
    filed rather than merely acknowledged."""
    token, _ = registered_node

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame(delay=[], doppler=[], snr=[], adsb_hex=[]))

    assert response.status_code == 202
    assert response.json()["accepted"] == 0
    ((_, frame),) = _queued()
    assert frame["delay"] == []


@pytest.mark.parametrize(
    "mismatch",
    [
        pytest.param({"snr": [1.0]}, id="short-snr"),
        pytest.param({"adsb_hex": []}, id="empty-adsb-hex"),
        pytest.param({"delay": [12.4, 30.1, 7.0]}, id="long-delay"),
    ],
)
async def test_parallel_arrays_of_different_lengths_are_rejected(registered_node, node_client, mismatch):
    """Rejected rather than truncated: the four arrays are one table on its side,
    so a shorter one means the frame does not say what it appears to."""
    token, _ = registered_node

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame(**mismatch))

    # 400 in the node error taxonomy, not the framework's 422: the contract
    # declares neither that status nor its body. See test_node_error_taxonomy.py.
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_body"
    assert _queued() == []


async def test_an_unknown_config_version_is_409(registered_node, node_client):
    token, _ = registered_node

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame(config_version=99))

    assert response.status_code == 409
    assert response.json() == {"error": "unknown_config_version"}
    assert _queued() == []


async def test_a_superseded_config_version_is_accepted_and_reported_stale(registered_node, node_client, node_session):
    """A version the server issued and has since replaced is not an unknown one.

    `node_configs` is append-only so the geometry a frame was computed under
    stays readable, which is what makes the frame interpretable. 409-ing every
    mismatch instead would leave `config_stale` on the ack unreachable, and
    would refuse the frames already in flight when a node PUTs a new
    configuration.
    """
    token, node_id = registered_node
    await _supersede_config(node_session, node_id)

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame(config_version=1))

    assert response.status_code == 202
    assert response.json() == {"accepted": 2, "config_stale": True, "streaming_allowed": True}
    assert len(_queued()) == 1


async def test_the_frame_carries_no_node_identifier(registered_node, node_client):
    """Attribution comes from the token, so a body that names a node is refused
    rather than raising a question about which of the two to believe."""
    token, _ = registered_node

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame(node_ref="nde000000000000"))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_body", "detail": "node_ref"}


async def test_the_detection_path_writes_nothing_to_the_database(registered_node, node_client, node_session):
    """Twenty-four writes a second at the fleet's ceiling, for data nothing reads."""
    token, node_id = registered_node

    for _ in range(3):
        assert node_client.post(DETECTION, headers=_auth(token), json=_frame()).status_code == 202

    # Every mutable column the path could plausibly touch, rather than the one
    # it is most likely to: `last_used_at` on the token is the tempting second
    # write, and checking only `nodes` would let it back in unnoticed.
    assert await node_session.scalar(select(Node.last_seen_at).where(Node.node_id == node_id)) is None
    assert await node_session.scalar(select(Node.active_config_version).where(Node.node_id == node_id)) == 1
    assert await node_session.scalar(select(NodeToken.last_used_at).where(NodeToken.node_id == node_id)) is None
    assert await node_session.scalar(select(func.count()).select_from(NodeConfig)) == 1


async def test_a_blocked_node_is_told_to_pause_rather_than_refused(registered_node, node_client, node_session):
    """A refusal is indistinguishable from a fault, and a node that believes
    itself faulty retries."""
    token, node_id = registered_node
    await _set_status(node_session, node_id, "blocked")

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.status_code == 202
    assert response.json()["streaming_allowed"] is False


async def test_a_blocked_node_s_frames_stay_out_of_the_pipeline(registered_node, node_client, node_session):
    """Telling it to pause is the courtesy; not filing its frames is the block."""
    token, node_id = registered_node
    await _set_status(node_session, node_id, "blocked")

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.json()["accepted"] == 0
    assert _queued() == []


async def test_a_frame_from_a_node_absent_from_the_pipeline_is_not_filed(registered_node, node_client):
    """Not filed, because frame_processor would solve it against the wrong geometry.

    `get_or_create_node_pipeline` falls back to the process-wide default
    pipeline for a node it holds no configuration for, so the frame would reach
    the map as a plausible detection against somebody else's receiver and
    transmitter, with an ack claiming it was accepted. Wrong data is worse than
    the gap, and the gap is at most one heartbeat interval wide.
    """
    token, _ = registered_node
    state.connected_nodes.clear()

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "config_stale": False, "streaming_allowed": True}
    assert _queued() == []


async def test_a_dropped_frame_is_counted_rather_than_only_logged(registered_node, node_client):
    """`frames_dropped` is the fleet's "we did not file this" counter, and a
    node streaming into a void with every metric flat is how the condition goes
    unnoticed."""
    token, _ = registered_node
    state.connected_nodes.clear()
    before = state.frames_dropped

    node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert state.frames_dropped == before + 1


async def test_the_node_absent_from_the_pipeline_is_not_told_to_stop(registered_node, node_client):
    """`streaming_allowed` stays true: the node is not blocked and there is
    nothing for it to do differently. Telling it to pause would leave it paused
    until an operator noticed, when its own next heartbeat fixes this."""
    token, _ = registered_node
    state.connected_nodes.clear()

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.json()["streaming_allowed"] is True


async def test_a_heartbeat_restores_filing_after_frames_were_dropped(registered_node, node_client):
    """The whole reason dropping is acceptable: the recovery path closes it."""
    token, _ = registered_node
    state.connected_nodes.clear()
    assert node_client.post(DETECTION, headers=_auth(token), json=_frame()).json()["accepted"] == 0

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())
    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.json()["accepted"] == 2
    assert len(_queued()) == 1


async def test_a_full_queue_is_a_dropped_frame_rather_than_an_error(registered_node, node_client, monkeypatch):
    """Latest wins on the node side, so a frame the server could not take is
    gone rather than owed. `accepted` says so; the status stays 202."""
    import asyncio

    token, _ = registered_node
    full = asyncio.Queue(maxsize=1)
    full.put_nowait(("filler", {}))
    monkeypatch.setattr(state, "frame_queue", full)

    response = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "config_stale": False, "streaming_allowed": True}


async def test_the_per_token_detection_limit_fires(registered_node, node_client, frozen_limiter):
    """Eight a second, sized against the 2 Hz contract ceiling rather than the
    ~1.1 Hz measured today: a node cannot exceed one frame per CPI, and sizing
    this against today's measurement would start refusing frames the day blah2
    gets faster."""
    token, _ = registered_node

    admitted = [node_client.post(DETECTION, headers=_auth(token), json=_frame()) for _ in range(8)]
    refused = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert [r.status_code for r in admitted] == [202] * 8
    assert refused.status_code == 429
    assert refused.json() == {"error": "rate_limited"}
    assert int(refused.headers["Retry-After"]) >= 1


async def test_the_route_binds_the_shared_limiter():
    """`frozen_limiter` patches the route's own name, which is only a fair test
    while that name is the process-wide instance. A second instance would be a
    second allowance."""
    from routes import node_stream
    from services import node_rate_limits

    assert node_stream.token_rate_limiter is node_rate_limits.token_rate_limiter


# ── heartbeat ────────────────────────────────────────────────────────────────


async def test_a_heartbeat_returns_the_whole_downlink(registered_node, node_client, node_session):
    """Restated in full every beat: a paused node makes no detection requests,
    so this response is the only thing it still hears."""
    token, node_id = registered_node

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"server_time", "config_stale", "streaming_allowed", "node_ref"}
    assert body["config_stale"] is False
    assert body["streaming_allowed"] is True
    assert body["node_ref"] == await node_session.scalar(select(Node.node_ref).where(Node.node_id == node_id))


async def test_server_time_is_rfc3339_utc_with_a_z(registered_node, node_client):
    """The one field a node measures its clock offset against, and a Pi 5 has no
    battery-backed RTC to be right without it."""
    token, _ = registered_node

    body = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat()).json()

    assert body["server_time"].endswith("Z")
    assert datetime.fromisoformat(body["server_time"]).tzinfo is not None


async def test_a_node_holding_no_config_version_is_heard_and_told_it_is_stale(registered_node, node_client):
    """Nullable since contract 1.1.0. A node that cannot build a configuration
    can never PUT one, and is the node most worth hearing from."""
    token, _ = registered_node

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(config_version=None))

    assert response.status_code == 200
    assert response.json()["config_stale"] is True


async def test_the_heartbeat_stamps_last_seen_and_the_node_list(registered_node, node_client, node_session):
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert await node_session.scalar(select(Node.last_seen_at).where(Node.node_id == node_id)) is not None
    assert state.connected_nodes[node_id]["last_heartbeat"] != ""


async def test_a_heartbeat_recovers_a_node_missing_from_the_pipeline(registered_node, node_client):
    """The state of a fresh worker process: registries empty, token still valid.

    Without this the node never comes back, because a spec-compliant client has
    no reason to re-register.
    """
    token, node_id = registered_node
    state.connected_nodes.clear()

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert response.status_code == 200
    assert node_id in state.connected_nodes
    assert state.connected_nodes[node_id]["config"]["rx_lat"] == pytest.approx(51.42)


async def test_a_recovered_node_is_stamped_on_the_same_beat_that_recovered_it(registered_node, node_client):
    """`register_with_pipeline` seeds an empty `last_heartbeat`, so a beat that
    recovers a node and leaves that empty would read as one that never beat."""
    token, node_id = registered_node
    state.connected_nodes.clear()

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert state.connected_nodes[node_id]["last_heartbeat"] != ""


async def test_a_node_with_no_active_configuration_still_gets_its_beat_answered(
    registered_node, node_client, node_session
):
    """Recovery needs a configuration and this node has none, which is exactly
    the case priming skips. The beat is the useful part and is answered anyway."""
    token, node_id = registered_node
    config = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id))).scalars().one()
    config.superseded_at = datetime.now(UTC)
    await node_session.commit()
    state.connected_nodes.clear()

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert response.status_code == 200
    assert node_id not in state.connected_nodes


async def test_a_blocked_node_is_told_to_pause_on_the_heartbeat(registered_node, node_client, node_session):
    token, node_id = registered_node
    await _set_status(node_session, node_id, "blocked")

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert response.status_code == 200
    assert response.json()["streaming_allowed"] is False


async def test_a_blocked_node_is_not_recovered_into_the_pipeline(registered_node, node_client, node_session):
    """Priming loads active nodes only, and recovery matches it: a block that a
    heartbeat undoes is not a block."""
    token, node_id = registered_node
    await _set_status(node_session, node_id, "blocked")
    state.connected_nodes.clear()

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert node_id not in state.connected_nodes


async def test_the_per_token_heartbeat_limit_fires(registered_node, node_client, frozen_limiter):
    """Thirty a minute: generous, because a heartbeat is needed precisely when a
    node is in trouble, but not unbounded."""
    token, _ = registered_node

    admitted = [node_client.post(HEARTBEAT, headers=_auth(token), json=_beat()) for _ in range(30)]
    refused = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert [r.status_code for r in admitted] == [200] * 30
    assert refused.status_code == 429
    assert refused.json() == {"error": "rate_limited"}


async def test_the_two_endpoints_do_not_share_one_allowance(registered_node, node_client, frozen_limiter):
    """Keyed on (node_id, endpoint), so a node streaming at its ceiling can still
    be heard from. The heartbeat is the one thing a node in trouble has left."""
    token, _ = registered_node
    for _ in range(8):
        node_client.post(DETECTION, headers=_auth(token), json=_frame())

    assert node_client.post(DETECTION, headers=_auth(token), json=_frame()).status_code == 429
    assert node_client.post(HEARTBEAT, headers=_auth(token), json=_beat()).status_code == 200


async def test_a_stale_config_version_is_reported_the_same_on_both_endpoints(
    registered_node, node_client, node_session
):
    """`config_stale` means one thing, so the two paths cannot disagree about it."""
    token, node_id = registered_node
    fresh_beat = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())
    fresh_frame = node_client.post(DETECTION, headers=_auth(token), json=_frame())

    await _supersede_config(node_session, node_id)

    stale_beat = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(config_version=1))
    stale_frame = node_client.post(DETECTION, headers=_auth(token), json=_frame(config_version=1))

    assert fresh_beat.json()["config_stale"] is False
    assert fresh_frame.json()["config_stale"] is False
    assert stale_beat.json()["config_stale"] is True
    assert stale_frame.json()["config_stale"] is True


# ── authentication ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [DETECTION, HEARTBEAT])
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": "Bearer not-a-token"}, id="unknown-token"),
        pytest.param({"Authorization": "Basic abc123"}, id="wrong-scheme"),
    ],
)
async def test_both_paths_need_a_live_bearer(registered_node, node_client, path, headers):
    """Requested `registered_node` so the failure is the credential rather than
    an empty database."""
    body = _frame() if path == DETECTION else _beat()

    response = node_client.post(path, headers=headers, json=body)

    assert response.status_code == 401
    assert _queued() == []
