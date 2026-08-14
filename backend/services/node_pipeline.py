"""Make a v1 node indistinguishable from a blah2_bridge node to the pipeline.

The bridge is the working reference: services/blah2_bridge.py registers a node
with three calls and pushes frames onto one queue. This does the same, so a v1
node reaches the map without anything downstream knowing the difference.
"""

import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import state
from core.nodes import Node, NodeConfig

logger = logging.getLogger(__name__)

# The pipeline expects three fields the v1 wire config does not carry.
# Values copied from services/blah2_bridge.py rather than invented.
_PIPELINE_DEFAULTS = {"doppler_min": -300, "doppler_max": 300, "min_doppler": 15}

# beam_azimuth_deg is passed through rather than defaulted: null is broadside
# and 0.0 is aimed due north, so substituting a value here would silently
# re-aim every node whose operator left it unset. resolve_beam_azimuth_deg in
# retina-analytics resolves the null.
_WIRE_FIELDS = (
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "fc_hz",
    "fs_hz",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
)


async def _pipeline_config(session: AsyncSession, node_id: str) -> dict:
    row = (
        (
            await session.execute(
                select(NodeConfig).where(NodeConfig.node_id == node_id, NodeConfig.superseded_at.is_(None))
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise ValueError(f"{node_id} has no active configuration")
    config = {field: getattr(row, field) for field in _WIRE_FIELDS}
    config.update(_PIPELINE_DEFAULTS)
    return config


async def register_with_pipeline(session: AsyncSession, node: Node) -> None:
    config = await _pipeline_config(session, node.node_id)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    with state.connected_nodes_lock:
        state.connected_nodes[node.node_id] = {
            "config_hash": config_hash,
            "config": config,
            "status": "active",
            "last_heartbeat": "",
            "peer": "v1",
            "is_synthetic": False,
            "capabilities": {"adsb_report": True},
        }
    state.node_analytics.register_node(node.node_id, config)
    state.node_associator.register_node(node.node_id, config)
    logger.info("node_api: registered %s with the pipeline", node.node_id)


async def prime_pipeline(session: AsyncSession) -> int:
    """Load every active node into the in-process registries. Returns how many.

    connected_nodes, node_analytics and node_associator all start empty in a fresh
    process, and only registration and PUT /config write to them. Without this, a
    deploy makes every node disappear from the pipeline, the heartbeat handler
    raises KeyError on connected_nodes, and nothing recovers: the node client is
    spec-compliant and will not re-register to fix it.
    """
    nodes = (await session.execute(select(Node).where(Node.status == "active"))).scalars().all()
    primed = 0
    for node in nodes:
        try:
            await register_with_pipeline(session, node)
        except ValueError:
            logger.warning("node_api: %s has no active configuration, not primed", node.node_id)
            continue
        primed += 1
    # WARNING rather than INFO deliberately. Under uvicorn the root logger sits
    # at WARNING, so an INFO line here is invisible in every deployed
    # environment, and this is the one line that says whether the fleet came
    # back after a deploy. Same trap that hid n2_unconfirmed (core/state.py).
    logger.warning("node_api: primed %d node(s) into the pipeline", primed)
    return primed


async def prime_pipeline_at_startup() -> int:
    """Prime from the application's own session, and never take startup down.

    Imported inside the function so the session maker is resolved at call time:
    module import order in main.py would otherwise bind it before tests can
    substitute one.

    A failure here costs the v1 fleet its pipeline membership until the next
    restart, which is bad but recoverable. Raising instead would abort the
    lifespan and take the whole API with it, including the blah2_bridge path
    this phase deliberately keeps running as its rollback.
    """
    import core.users
    from services.alerting import send_alert

    try:
        async with core.users.async_session_maker() as session:
            return await prime_pipeline(session)
    except Exception as exc:
        logger.exception("node_api: priming the pipeline failed, no v1 node is registered")
        send_alert("node_priming_failed", "Node pipeline priming failed at startup", {"error": str(exc)})
        return 0


def submit_frame(node_id: str, frame: dict) -> bool:
    """Push one frame. False means the queue was full and the frame was dropped."""
    frame["_node_id"] = node_id
    try:
        state.frame_queue.put_nowait((node_id, frame))
    except Exception:
        state.bump_counter("frames_dropped")
        return False
    return True
