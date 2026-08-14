"""The two streaming-tag paths: POST /v1/nodes/detection and /v1/nodes/heartbeat.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handlers land here; detection answers 202, heartbeat 200.

Detection is the hot path and writes nothing to the database. Twelve nodes at
the contract's 2 Hz ceiling would be twenty-four writes a second for data
nothing reads, so a frame costs two indexed reads (the token, then the node row)
and a queue push, and the node's own record of itself is updated once a minute
by the heartbeat instead.

Neither handler derives anything from an assumed send interval. A node has no
send timer: it POSTs once per frame blah2 produces, so the arrival rate is the
radar's frame rate and moves with load, clutter and the board. 2 Hz is the
ceiling the rate limit is sized against and nothing else.
"""

import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import state
from core.nodes import Node, NodeConfig
from core.users import get_async_session
from routes.node_schemas import (
    DetectionAck,
    DetectionFrame,
    ErrorBody,
    HeartbeatRequest,
    HeartbeatResponse,
)
from services.node_auth import bearer_node
from services.node_pipeline import register_with_pipeline, submit_frame
from services.node_rate_limits import Refusal, token_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter()

# What FastAPI publishes for the failures both handlers can return. The 401 is
# raised by the `bearer_node` dependency rather than here, so it is documented
# rather than emitted below.
_ERROR_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorBody, "description": "Token bad, revoked or expired."},
    429: {"model": ErrorBody, "description": "Rate limited. Honour `Retry-After`."},
}


def _refused(refusal: Refusal) -> JSONResponse:
    """Render a limiter refusal, which is data until a route makes it a response."""
    return JSONResponse(
        status_code=refusal.status_code,
        content=refusal.body,
        headers={"Retry-After": str(refusal.retry_after_s)},
    )


async def _authenticated_node(session: AsyncSession, node_id: str) -> Node:
    """The node row behind a live bearer token.

    `node_tokens` carries a foreign key to `nodes`, so a live token with no node
    behind it takes the row disappearing while the token stayed valid. It is
    still cheaper to answer 401 than to let the hot path raise: the credential
    genuinely no longer resolves to anything.
    """
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return node


async def _version_was_ever_issued(session: AsyncSession, node_id: str, version: int) -> bool:
    """Whether this node was ever issued this configuration version.

    Only reached when a frame disagrees with the node's active version, so the
    ordinary frame never pays for it: a frame stamped with the active version is
    known to exist without being looked up.

    A superseded version is still a known one, and that is the distinction the
    contract draws. `node_configs` is append-only precisely so the geometry a
    frame was computed under stays readable after it is replaced, so a frame
    carrying one is interpretable and is accepted with `config_stale` set. Only
    a version the server never issued is uninterpretable, and that is what the
    409 names. Answering 409 to every mismatch instead would make `config_stale`
    on the ack unreachable, and would 409 the frames already in flight when a
    node PUTs a new configuration, which is a re-PUT loop rather than a recovery.
    """
    row = await session.execute(
        select(NodeConfig.id).where(NodeConfig.node_id == node_id, NodeConfig.version == version).limit(1)
    )
    return row.first() is not None


def _pipeline_frame(frame: DetectionFrame) -> dict:
    """The wire frame in the shape services/blah2_bridge.py puts on the queue.

    `timestamp` is milliseconds because that is what the queue's readers expect.
    `delay` needs no conversion: it is microseconds on the wire, where the bridge
    has to convert from kilometres.

    `adsb_hex` travels under its own key rather than the bridge's `adsb`, which
    frame_processor reads as position reports. The contract's array is an
    association and carries no lat/lon, so filing it there would be filing an
    empty position for every detection.

    `seq` and `boot_id` are carried rather than dropped at the boundary: they are
    the pair the server counts loss against, and only meaningful together, since
    `seq` restarts from zero with the process.
    """
    return {
        "timestamp": int(frame.t * 1000),
        "delay": list(frame.delay),
        "doppler": list(frame.doppler),
        "snr": list(frame.snr),
        "adsb_hex": list(frame.adsb_hex),
        "seq": frame.seq,
        "boot_id": frame.boot_id,
        "config_version": frame.config_version,
    }


# A node streams at up to 2 Hz and the condition below clears on its next
# heartbeat, so an unthrottled warning would be a hundred lines about a fault
# that was fixing itself. One line a minute per node says the same thing.
_UNREGISTERED_LOG_INTERVAL_S = 60.0
_unregistered_logged_at: dict[str, float] = {}


def _warn_unregistered(node_id: str) -> None:
    now = time.monotonic()
    if now - _unregistered_logged_at.get(node_id, 0.0) < _UNREGISTERED_LOG_INTERVAL_S:
        return
    _unregistered_logged_at[node_id] = now
    # WARNING rather than INFO, for the reason prime_pipeline gives: under
    # uvicorn the root logger sits at WARNING, so an INFO line here would be
    # invisible in every deployed environment, and this is the line that says a
    # node's data is going nowhere.
    logger.warning(
        "node_api: %s is absent from the pipeline registries, dropping its frames until its next heartbeat",
        node_id,
    )


def _file_frame(node_id: str, frame: DetectionFrame) -> int:
    """Queue one frame, and report how many detections were filed.

    Zero in both of the ways this declines, because v1 files a frame whole or
    not at all: the count is the array length or nothing.

    A node absent from `state.connected_nodes` is declined rather than queued.
    frame_processor has no geometry for such a node and falls back to the
    process-wide default pipeline, so the frame would be solved against
    somebody else's receiver and transmitter and reach the map as a plausible
    detection in the wrong place, while the ack claimed it was accepted.
    Declining costs at most one heartbeat interval of this node's data, and the
    next beat restores it by re-registering the node. Queueing costs
    correctness, silently, which is the worse trade.

    Recovery deliberately does not happen here. It needs a database read and a
    write to the registries, and this is the path that runs at the fleet's frame
    rate; the heartbeat carries it once a minute instead.
    """
    with state.connected_nodes_lock:
        known_to_pipeline = node_id in state.connected_nodes
    if not known_to_pipeline:
        # The same counter submit_frame bumps when the queue is full: both mean
        # a frame the server did not file, and neither should read as ingested.
        state.bump_counter("frames_dropped")
        _warn_unregistered(node_id)
        return 0
    if not submit_frame(node_id, _pipeline_frame(frame)):
        return 0
    return len(frame.delay)


@router.post(
    "/detection",
    status_code=202,
    response_model=DetectionAck,
    responses={
        **_ERROR_RESPONSES,
        409: {"model": ErrorBody, "description": "Unknown `config_version`. `PUT /nodes/config`, then resume."},
    },
    summary="Send one detection frame.",
)
async def post_detection(
    frame: DetectionFrame,
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
):
    """Push one frame onto the pipeline, attributed to the token that carried it.

    The frame holds no node identifier at all: attribution comes from the bearer,
    which is the whole reason the token exists, and `extra="forbid"` on the model
    means a body that tries to name a node is a 422 rather than a question about
    which of the two to believe.

    A node missing from `state.connected_nodes` is neither recovered here nor
    filed: see `_file_frame`. The heartbeat does the recovery, once a minute,
    off the hot path.
    """
    refusal = token_rate_limiter.admit(node_id, "detection")
    if refusal is not None:
        return _refused(refusal)

    node = await _authenticated_node(session, node_id)

    config_stale = frame.config_version != node.active_config_version
    if config_stale and not await _version_was_ever_issued(session, node_id, frame.config_version):
        return JSONResponse(status_code=409, content=ErrorBody(error="unknown_config_version").model_dump())

    # A blocked node is told to pause rather than refused, for the same reason
    # the heartbeat does it: a refusal is indistinguishable from a fault, and a
    # node that believes itself faulty retries. Its frames stay out of the
    # pipeline in the meantime, which is what the block is for.
    streaming_allowed = node.status == "active"
    accepted = _file_frame(node_id, frame) if streaming_allowed else 0
    return DetectionAck(accepted=accepted, config_stale=config_stale, streaming_allowed=streaming_allowed)


@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    responses=_ERROR_RESPONSES,
    summary="Report liveness and health.",
)
async def post_heartbeat(
    beat: HeartbeatRequest,
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
):
    """Stamp the node as seen and return the whole downlink.

    Every field is restated on every beat rather than sent on change, because a
    paused node makes no detection requests and this response is the only thing
    it still hears. `health`, `versions` and `errors` are accepted and not read:
    the server decides whether a node is working from its own record of frame
    arrivals, since `blah2: "up"` reads the same on a wedged node as a working
    one.
    """
    refusal = token_rate_limiter.admit(node_id, "heartbeat")
    if refusal is not None:
        return _refused(refusal)

    node = await _authenticated_node(session, node_id)
    now = datetime.now(UTC)
    node.last_seen_at = now

    # A node the in-process registries have lost is put back rather than raising.
    # Startup priming covers the ordinary restart; this covers a node primed into
    # a different worker process, or skipped for want of an active configuration
    # it has since PUT. Without it such a node never returns, because a
    # spec-compliant client has no reason to re-register. A blocked node is left
    # out, matching what priming itself loads.
    with state.connected_nodes_lock:
        known_to_pipeline = node_id in state.connected_nodes
    if not known_to_pipeline and node.status == "active":
        try:
            await register_with_pipeline(session, node)
        except ValueError:
            # Still no active configuration. The beat is the useful part and is
            # answered anyway: this is the node most worth hearing from, and it
            # is the one that cannot PUT its way out on its own.
            logger.warning("node_api: %s has no active configuration, not recovered into the pipeline", node_id)

    # Taken after the recovery above rather than around it: the lock is a plain
    # threading lock and register_with_pipeline awaits, so holding it across that
    # call would block the loop's other tasks on a database read.
    with state.connected_nodes_lock:
        entry = state.connected_nodes.get(node_id)
        if entry is not None:
            entry["last_heartbeat"] = now.isoformat()

    await session.commit()
    return HeartbeatResponse(
        server_time=now,
        # `null` is a version too: a node that holds none has nothing the server
        # issued, so a server that holds one has something for it to fetch.
        config_stale=beat.config_version != node.active_config_version,
        streaming_allowed=node.status == "active",
        node_ref=node.node_ref,
    )
