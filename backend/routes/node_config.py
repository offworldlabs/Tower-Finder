"""PUT /v1/nodes/config, where a node resends its full configuration.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handler lands here and answers 200 on success.

Distinct from `services/node_config.py`, which validates a payload, and from
`services/node_config_store.py`, which versions it. Import both by their full
paths to keep them apart from this module.

Registration shares both, so the version this endpoint returns for an unchanged
resend is the one POST /v1/nodes/register would return.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node
from core.users import get_async_session
from routes.node_schemas import ConfigResponse, ErrorBody
from services.node_auth import bearer_node
from services.node_config import ConfigInvalid, validate_config
from services.node_config_store import upsert_config
from services.node_pipeline import register_with_pipeline

logger = logging.getLogger(__name__)

router = APIRouter()


# `Error.detail`'s bound, from the contract and from ErrorBody in routes/node_schemas.py.
_MAX_DETAIL = 512


def _error(status_code: int, error: str, detail: str | None = None) -> JSONResponse:
    """The contract's `Error` shape. `detail` is dropped when absent, not nulled.

    Truncated rather than passed through: on an unknown field the detail is a key
    the caller chose, and a key longer than the bound would fail ErrorBody's own
    validation inside the handler and turn a 400 into a 500. A node retries a 5XX
    and does not retry a 400, so the body that says "this will never be accepted"
    is exactly the one that must not arrive as a server error.
    """
    if detail is not None:
        detail = detail[:_MAX_DETAIL]
    return JSONResponse(ErrorBody(error=error, detail=detail).model_dump(), status_code=status_code)


@router.put(
    "/config",
    response_model=ConfigResponse,
    responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
)
async def put_config(
    request: Request,
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
) -> ConfigResponse | JSONResponse:
    """Store the configuration if it differs from the active one, and return the active version.

    The body is read here rather than declared as a parameter, and the bearer is
    resolved before it is touched. A body declared on the signature is parsed and
    validated by FastAPI ahead of the handler, which would put a body-shaped
    refusal in front of identity resolution and make the difference between it and
    a 401 an oracle for which tokens are live. Resolving the bearer as a dependency
    keeps that ordering — dependencies run first, and nothing here declares a body
    for FastAPI to parse. It is also why the payload stays an untyped dict, as
    `RegisterRequest.config` does: `services/node_config.py` owns configuration
    validation, and it is called once identity has resolved.
    """
    try:
        payload = await request.json()
    except ValueError:
        # A body that is not JSON at all fails validation the same way a body that is
        # JSON but not a configuration does, and for the node the remedy is the same:
        # resending it unchanged will not help.
        return _error(400, "invalid_config", "config")

    try:
        config = validate_config(payload)
    except ConfigInvalid as exc:
        return _error(400, "invalid_config", exc.field)

    version = await upsert_config(session, node_id, config)
    node = await session.get(Node, node_id)
    changed = node.active_config_version != version
    node.active_config_version = version
    await session.commit()

    # After the commit, so the pipeline is only told what the database has already
    # accepted; registering first and then failing to commit would leave the solver
    # using geometry no row records.
    if changed:
        logger.info("node_api: %s moved to configuration version %d", node_id, version)
        await _hand_to_pipeline(session, node, version)

    return ConfigResponse(config_version=version)


async def _hand_to_pipeline(session: AsyncSession, node: Node, version: int) -> None:
    """Put the new geometry in front of the solver, and alert rather than fail if it will not go.

    The version is stored and committed by this point, so the node's own job is done
    and a 500 would only have it resend a configuration the server already holds. Same
    shape, and the same reasoning, as prime_pipeline_at_startup.

    Be clear about what a failure costs, because it is not membership. Registration
    writes state.connected_nodes first and the analytics and associator registries
    after, so a failure in either of those leaves connected_nodes advertising the new
    configuration and its hash while the associator still holds the old geometry: the
    three disagree until the next restart primes them from the table. Nor is it
    retried. The version is committed, so the node row already reads it, and the next
    identical resend finds nothing changed and never reaches here. The alert is
    therefore the whole of the recovery path, which is why it carries the version.

    One failure is live rather than hypothetical: retina-analytics reads
    `config.get("beam_width_deg", 41)`, so an explicit null passes its default by, and
    the unchanged-geometry comparison in association.py then subtracts it. Every node
    in the fleet sends a null width under contract 1.1.1. Tracked in 86cb5dakr, with
    the ordering above; neither is this endpoint's to fix.
    """
    from services.alerting import send_alert

    try:
        await register_with_pipeline(session, node)
    except Exception as exc:
        logger.exception("node_api: %s reached version %d but not the pipeline", node.node_id, version)
        send_alert(
            "node_config_handoff_failed",
            f"{node.node_id} stored configuration version {version} but did not reach the pipeline",
            {"node_id": node.node_id, "config_version": version, "error": str(exc)},
        )
