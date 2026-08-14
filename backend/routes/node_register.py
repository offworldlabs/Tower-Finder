"""POST /v1/nodes/register, the one-off handshake that mints a node's token.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handler lands here and answers 200 on success.

Two orderings in the handler are requirements rather than preferences, and each
is wrong in an obvious-looking alternative:

The rate limiter runs before the Mender call (D26), so an unauthenticated caller
cannot drive traffic at the Mender tenant by looping on registration.

Configuration validation runs after identity resolution. A 400 reachable ahead
of it would turn the difference between 400 and 403 into an oracle for which
node identities exist, which is the whole reason every refusal here shares one
body. `RegisterRequest.config` is untyped for the same reason: a Pydantic model
would 422 ahead of the handler and reintroduce the oracle from the framework
side.

The endpoint is unauthenticated, so the only thing standing between a stranger
and a node takeover is Mender acceptance plus the registration limiter. See the
re-registration comment below for what that buys and what it does not.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node
from core.users import get_async_session
from routes.node_schemas import Agreements, ErrorBody, RegisterRequest, RegisterResponse
from services.mender import MenderUnreachable, lookup_device
from services.node_auth import mint_node_ref, mint_token, revoke_tokens
from services.node_config import ConfigInvalid, validate_config
from services.node_config_store import upsert_config
from services.node_pipeline import register_with_pipeline
from services.node_rate_limits import Refusal, registration_limiter
from services.node_refusals import REFUSAL_BODY, refusal_retry_after

router = APIRouter()

# The contract's bound on Error.detail, which ErrorBody enforces.
ERROR_DETAIL_MAX = 512


def _render(refusal: Refusal) -> JSONResponse:
    """One place builds a refusal response, so no caller can vary the shape.

    The limiter hands back its refusal as data, already carrying the shared body
    and a Retry-After drawn from the same jittered range as every other class.
    """
    return JSONResponse(
        refusal.body,
        status_code=refusal.status_code,
        headers={"Retry-After": str(refusal.retry_after_s)},
    )


def _refuse() -> JSONResponse:
    """Unknown device, not yet accepted, and Mender unreachable, identically."""
    return _render(Refusal(403, dict(REFUSAL_BODY), refusal_retry_after()))


def _utc(moment: datetime) -> datetime:
    """Normalise an aware timestamp to UTC before it reaches a column.

    SQLite stores no offset, so an aware value is written as its own wall clock
    and read back as the UTC every other timestamp in these tables is: an
    acceptance sent as 09:12+02:00 would be filed as 09:12 UTC, two hours late,
    and could end up appearing to postdate the registration it authorised. The
    models accept an offset because the contract's `date-time` carries one; this
    is where it stops being one.
    """
    return moment.astimezone(UTC)


def _apply_agreements(node: Node, agreements: Agreements) -> None:
    """The record of what the owner accepted, written out rather than implied.

    Rewritten on every registration, not only the first: the three records are
    separately versioned because they are withdrawn separately, and a board that
    comes back having withdrawn its publication choice must not be left carrying
    the old one.
    """
    node.licence_version = agreements.licence.version
    node.licence_accepted_at = _utc(agreements.licence.accepted_at)
    node.remote_management_version = agreements.remote_management.version
    node.remote_management_accepted_at = _utc(agreements.remote_management.accepted_at)
    node.publication = agreements.publication.choice
    node.publication_version = agreements.publication.version
    node.publication_chosen_at = _utc(agreements.publication.accepted_at)


# response_model rather than inference from the return annotation: FastAPI
# cannot build a response field from a union with a Response in it, and the
# refusals are rendered as JSONResponse so they carry a Retry-After header.
@router.post("/register", response_model=RegisterResponse)
async def register_node(
    request: RegisterRequest,
    session: AsyncSession = Depends(get_async_session),
) -> RegisterResponse | JSONResponse:
    # Resolved at call time rather than bound at import, so a test's patch of
    # services.alerting.send_alert is the function this calls. Same reason
    # services/node_pipeline.py imports it inside the function body.
    from services.alerting import send_alert

    # D26: before the Mender call, and keyed on the node_id in the body, which is
    # the only identity an unauthenticated caller offers. The model has already
    # held it to `^ret[0-9a-f]{8}$`, which is also what keeps an empty one away
    # from lookup_device: that matches on `identity_data["node_id"]`, and Mender
    # records carrying no node_id read as the empty string, so `lookup_device("")`
    # would resolve to the first of them rather than to nothing.
    #
    # Every admitted attempt is counted, including one that turns out to name an
    # identity Mender does not know or has not accepted. The contract says the
    # counters exclude attempts that could never have succeeded, which cannot be
    # done from ahead of the lookup: crediting an attempt back once Mender has
    # answered is the same as not limiting the lookup at all, which is what D26
    # asks for. A node waiting on Mender acceptance therefore spends its
    # allowance while it waits. See 86cb5dcra.
    refusal = registration_limiter.admit(request.node_id)
    if refusal is not None:
        return _render(refusal)

    try:
        device = await lookup_device(request.node_id)
    except MenderUnreachable:
        # The one alert that distinguishes "could not ask" from "does not know".
        # Both are this same 403 on the wire, but only one of them means every
        # enrolment in the fleet is blocked. There is no transaction open here,
        # so nothing is pending that this could alert ahead of.
        send_alert("mender_unreachable", f"registration for {request.node_id} could not reach Mender")
        return _refuse()
    if device is None or device.auth_status != "accepted":
        return _refuse()

    # Identity has resolved, so a 400 is now safe to return.
    try:
        config = validate_config(request.config)
    except ConfigInvalid as exc:
        # `detail` is the field name rather than a sentence: retina-gui puts it
        # next to the input it belongs to and owns the wording there. Built
        # through ErrorBody rather than as a literal so the contract's `Error`
        # shape and its bounds are enforced here as they are everywhere else.
        #
        # Truncated to that bound rather than trusted to fit. An unknown key is
        # named back to the caller, and a key is caller-supplied JSON: 513
        # characters of it inside the 8 KiB body cap would otherwise fail
        # ErrorBody's own validation and turn a 400 into a 500.
        detail = exc.field[:ERROR_DETAIL_MAX]
        return JSONResponse(ErrorBody(error="invalid_config", detail=detail).model_dump(), status_code=400)

    # One transaction from here. Every write below flushes and none of them
    # commits, so a failure part-way cannot leave a node whose previous token is
    # revoked and whose new one was never written.
    try:
        node, reissue, version, token = await _write_registration(session, request, config)
    except IntegrityError:
        # Two registrations for one node_id can interleave: the Mender lookup
        # above is an await on a network call, so both can read no row, and the
        # loser's flush then hits the nodes primary key or the unique constraint
        # on (node_id, version). Refused with the shared body rather than left to
        # become a 500, since the node's own retry finds the row the winner
        # committed and takes the re-registration path.
        #
        # Any integrity violation is read as a lost race rather than only those
        # two constraints, which is safe because the transaction writes five
        # known rows and nothing else. A sixth write added here would want that
        # revisited: it would be turned into a routine refusal rather than
        # surfacing.
        await session.rollback()
        return _refuse()

    # Everything that is not a database write happens after the commit, so a
    # rolled back registration neither alerts about a revocation that did not
    # happen nor leaves the pipeline holding a node the database does not.
    if reissue:
        send_alert("registration_held", f"{request.node_id} re-registered; previous token revoked")
    # Active nodes only, matching prime_pipeline: the pipeline entry is what puts
    # a node on the map, and it is written with status "active" whatever the row
    # says, so handing it a blocked board would undo the block everywhere except
    # the one place that reads node.status.
    if node.status == "active":
        await register_with_pipeline(session, node)
    return RegisterResponse(
        token=token,
        node_ref=node.node_ref,
        config_version=version,
        server_time=datetime.now(UTC),
    )


async def _write_registration(
    session: AsyncSession,
    request: RegisterRequest,
    config: dict[str, Any],
) -> tuple[Node, bool, int, str]:
    """The node, its agreements, its configuration version and its token, once.

    Returns the node, whether this was a re-registration, the version the node
    now holds and its new bearer. Committing between any two of these would let
    a crash leave a board whose previous token is dead and whose new one was
    never written, so the commit is here and there is exactly one.
    """
    node = await session.get(Node, request.node_id)
    reissue = node is not None
    if node is None:
        node = Node(node_id=request.node_id, node_ref=mint_node_ref(), board_model=request.board_model)
        session.add(node)
        # Flushed before upsert_config: node_configs.node_id is a foreign key and
        # SQLite has PRAGMA foreign_keys=ON (core/users.py).
        await session.flush()
    else:
        # Re-registration is allowed rather than gated on an operator
        # reactivation, which this build has no way to trigger and which would
        # therefore brick any reflashed board permanently. The cost is that
        # anyone who can enrol a Mender key under a known node_id can take a node
        # over; the alert below is what makes that visible rather than silent,
        # and the registration limiter is what bounds the rate. See 86cb4w4uv.
        #
        # node.status is deliberately left alone. A board an operator blocked or
        # retired comes back blocked or retired, since the detection handler
        # derives streaming_allowed from it: resetting it to active here would
        # make a reflash the way to undo an operator's decision.
        node.board_model = request.board_model
        await revoke_tokens(session, request.node_id, reason="reflash")

    _apply_agreements(node, request.agreements)
    # The version the server holds, which is 1 only for a node it has not seen.
    # Telling a returning board 1 when the server holds 4 gives it a 409 on every
    # frame afterwards.
    version = await upsert_config(session, request.node_id, config)
    node.active_config_version = version
    token = await mint_token(session, request.node_id)
    await session.commit()
    return node, reissue, version, token
