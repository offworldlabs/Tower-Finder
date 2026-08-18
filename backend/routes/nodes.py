"""The v1 node API: prefix, tags, body caps and error taxonomy for the four node
endpoints.

The handlers live in three sibling modules mounted below, so that work on one
endpoint never touches another's lines. This module holds only the wiring.

The exception handlers are here rather than in an endpoint module because only
an application can carry one, and this is the module that owns what "the node
API" means.
"""

import re
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from routes.node_config import router as node_config_router
from routes.node_register import router as node_register_router
from routes.node_schemas import ErrorBody
from routes.node_stream import router as node_stream_router

NODE_PATH_PREFIX = "/v1/nodes"

router = APIRouter(prefix=NODE_PATH_PREFIX, tags=["nodes"])

router.include_router(node_register_router)
router.include_router(node_config_router)
router.include_router(node_stream_router)

# Per-path request body caps, transcribed from `nodes_api_v1.yml` ("Request
# bodies are size-capped"): 8 KiB for registration, heartbeat and configuration,
# 64 KiB for a detection frame. The array and string bounds in the spec's
# schemas are the same limits expressed per field. Keys are full app paths,
# since the contract's server URL already carries the `/v1` prefix.
NODE_BODY_LIMITS: dict[str, int] = {
    "/v1/nodes/register": 8 * 1024,
    "/v1/nodes/config": 8 * 1024,
    "/v1/nodes/heartbeat": 8 * 1024,
    "/v1/nodes/detection": 64 * 1024,
}


# ── The error taxonomy ───────────────────────────────────────────────────────
#
# Every refusal under the prefix carries the contract's `Error`, whose required
# key is `error`. Two framework responses were reaching the wire unconverted:
# the 401 `bearer_node` raises, and the 422 a request model raises before any
# handler runs. Both are `{"detail": ...}`, which the node client reads as an
# unknown error, and the 422 is not even a status the contract declares.
#
# Scoped to the prefix rather than global, and the delegation below is what
# scopes it: the rest of this API is not written to this taxonomy and its
# callers parse FastAPI's shape.

# `Error.error`'s and `Error.detail`'s bounds, which ErrorBody enforces.
_MAX_ERROR = 64
_MAX_DETAIL = 512

# A slug the taxonomy can use as-is. Starlette's own detail is prose ("Not
# Found"), so the status phrase is used for those instead.
_SLUG = re.compile(r"[a-z][a-z0-9_]*")


def _under_the_node_api(request: Request) -> bool:
    # scope["path"] rather than request.url.path, for the reason LimitUploadSize
    # gives: the latter rebuilds and reparses a URL object per request.
    #
    # Exact match or a `/`-bounded child of the prefix, not a bare startswith:
    # the latter would also claim a hypothetical /v1/nodesXYZ, and this handler
    # decides whether a route answers in the node taxonomy or the framework's.
    path = request.scope["path"]
    return path == NODE_PATH_PREFIX or path.startswith(NODE_PATH_PREFIX + "/")


def _body(error: str, detail: str | None = None) -> dict[str, Any]:
    """The contract's `Error`, built through the model so its bounds apply."""
    return ErrorBody(error=error[:_MAX_ERROR], detail=detail).model_dump()


def _field(exc: RequestValidationError) -> str:
    """The first offending field's path, and nothing else.

    Deliberately not the message, the input, or pydantic's documentation URL,
    all three of which FastAPI's own rendering includes. The registration
    handler's refusals are opaque so that a caller cannot learn which node
    identities exist, and this runs ahead of identity resolution, so a body that
    said more than which key was wrong would be the one place that leaked. An
    unknown key is also caller-supplied text, and echoing it back unbounded is
    how a 400 becomes a 500 (see the same truncation in routes/node_register.py).
    """
    first = exc.errors()[0]
    if first.get("type") == "json_invalid":
        # The location is a byte offset into a body that did not parse, which
        # names nothing a node can act on.
        return "body"
    location = first.get("loc", ())
    # The leading "body" is the same on every one of these and says nothing:
    # none of the four endpoints declares a query or path parameter.
    if location and location[0] == "body":
        location = location[1:]
    return ".".join(str(part) for part in location)[:_MAX_DETAIL] or "body"


async def _validation_refusal(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A malformed body is a 400 in the taxonomy rather than FastAPI's 422.

    `invalid_body` rather than the `invalid_config` the handlers raise, because
    the two are different faults and a node should be able to tell them apart: a
    node told `invalid_config` by a mis-serialised detection frame would resend
    a configuration that was never the problem. On registration both are 400,
    which is what the contract's "configuration or agreement records failed
    validation" asks for — that sentence used to have two answers, since a bad
    config value reached the handler and a malformed `agreements` block did not.
    """
    if not _under_the_node_api(request):
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(_body("invalid_body", _field(exc)), status_code=400)


async def _http_refusal(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Anything raising a bare HTTPException under the prefix, chiefly the 401.

    Covers Starlette's own 404 and 405 as well as `bearer_node`, so a node that
    holds a path this server no longer routes is answered in the shape it parses
    rather than in prose.
    """
    if not _under_the_node_api(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else ""
    slug = detail if _SLUG.fullmatch(detail) else _status_slug(exc.status_code)
    return JSONResponse(_body(slug), status_code=exc.status_code, headers=exc.headers)


def _status_slug(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    except ValueError:
        return "error"


def install_error_handlers(app: FastAPI) -> None:
    """Put the taxonomy in front of the two framework responses that escape it.

    Registered on the application because an APIRouter cannot carry an exception
    handler: by the time one runs, the router that would have scoped it is no
    longer in the picture. Both handlers therefore delegate to FastAPI's own for
    anything outside the prefix.
    """
    app.add_exception_handler(RequestValidationError, _validation_refusal)
    app.add_exception_handler(StarletteHTTPException, _http_refusal)
