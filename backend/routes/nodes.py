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
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from routes.node_config import router as node_config_router
from routes.node_register import router as node_register_router
from routes.node_responses import NODE_BODY_LIMITS as NODE_BODY_LIMITS
from routes.node_schemas import ErrorBody
from routes.node_stream import router as node_stream_router

NODE_PATH_PREFIX = "/v1/nodes"

# The node API's own version, which the generated contract carries and which a
# breaking change has to raise. Not the application's: the rest of this API has
# no consumer holding a version, while the node client and the conformance
# harness are built against a pinned one.
#
# 1.1.2 succeeds the hand-written nodes_api_v1.yml at 1.1.1, and is a patch
# rather than a minor bump because nothing here is new API surface. No endpoint,
# no field, no capability: what the document gained is a description of what the
# server already did and 1.1.1 failed to say, which is the bearer scheme, the
# `x-` retry vocabulary, `Retry-After`, 413, 5XX and 400 on the streaming paths.
# The only declarations it drops are the 429s on registration and configuration,
# and no client can have depended on either, since the server has never emitted
# one. See 86cb6d7cq for the configuration one, which comes back with the limit.
#
# Publishing NodeConfig would be the minor bump, since that is the one thing
# here a client cannot already do (86cb6d7he).
NODE_API_VERSION = "1.1.2"

# No tag here: each sub-router carries the contract's own grouping, since those
# are what a generated client is built around.
router = APIRouter(prefix=NODE_PATH_PREFIX)

# Tag order and prose for the published contract, in the order a node meets them.
NODE_API_TAGS = [
    {"name": "registration", "description": "One-off handshake that mints the node's bearer token."},
    {"name": "streaming", "description": "The hot path, plus the liveness signal that runs alongside it."},
    {"name": "configuration", "description": "Receiver and transmitter geometry, versioned by the server."},
]

# Where the contract is served, for the client generated from it. Declared here
# rather than on the FastAPI application, which would point the interactive docs
# of every local and test process at production.
#
# The paths in this document carry the `/v1` themselves, so these are origins:
# nginx proxies `location /` straight through on the api vhost, and the
# resulting URL is the one the frozen contract described as a `/v1` server
# joined to a `/nodes/...` path.
NODE_API_SERVERS = [
    {"url": "https://api.retina.fm", "description": "Production ingest."},
    {"url": "https://staging-api.retina.fm", "description": "Staging ingest."},
]

router.include_router(node_register_router)
router.include_router(node_config_router)
router.include_router(node_stream_router)

# NODE_BODY_LIMITS itself is first-class in routes/node_responses.py, where the
# three endpoint modules above can also reach it without a cycle back through
# this module. Imported here (explicit self-alias, so the re-export is not
# read as an unused import) for main.py's LimitUploadSize middleware and for
# tests, both of which already know this module as where the node API's wiring
# lives.


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

# FastAPI's own detail when a request body cannot be read at all: a transport
# failure such as a client disconnecting mid-upload, not a JSON syntax error
# (which raises RequestValidationError and lands in _validation_refusal
# instead). Confirmed in fastapi.routing.get_request_handler, whose bare
# `except Exception` around the body read wraps anything but HTTPException and
# json.JSONDecodeError in this exact prose, as HTTPException(400, ...). It has
# spaces and capitals, so it never matches _SLUG, and the taxonomy's own
# fallback would then publish an undocumented `bad_request`. A node's remedy is
# the same as for a body that failed the schema, so it wears that slug instead.
_BODY_READ_FAILURE = "There was an error parsing the body"


def is_node_path(path: str) -> bool:
    """Exact match or a "/"-bounded prefix, not a bare startswith: the latter
    would also match a hypothetical /v1/nodesXYZ, which is not this API.

    A pure string predicate, shared with scripts/generate_openapi.py, which
    filters the generated document's paths down to the node API with this same
    rule rather than its own copy: the published scope and the runtime scope
    are one thing, not two that can drift apart.
    """
    return path == NODE_PATH_PREFIX or path.startswith(NODE_PATH_PREFIX + "/")


def _under_the_node_api(request: Request) -> bool:
    # scope["path"] rather than request.url.path, for the reason LimitUploadSize
    # gives: the latter rebuilds and reparses a URL object per request.
    return is_node_path(request.scope["path"])


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
    rather than in prose. Also covers FastAPI's own body-read failure, which is
    prose too and is remapped onto `invalid_body` rather than left to the
    generic status-phrase fallback.
    """
    if not _under_the_node_api(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else ""
    if exc.status_code == 400 and detail == _BODY_READ_FAILURE:
        slug = "invalid_body"
    else:
        slug = detail if _SLUG.fullmatch(detail) else _status_slug(exc.status_code)
    return JSONResponse(_body(slug), status_code=exc.status_code, headers=exc.headers)


async def _server_error(request: Request, exc: Exception) -> Response:
    """An exception nothing else here caught: not an `HTTPException`, not a
    validation error, just whatever a handler or its dependencies raised.

    Registered against bare `Exception`, which Starlette routes to
    `ServerErrorMiddleware` rather than the `ExceptionMiddleware` the two
    handlers above sit on. That middleware always re-raises the original
    exception once whatever is returned here has been sent, so the traceback
    still reaches the server log exactly as an uncaught exception's would; this
    handler does not need to log it again.

    Delegating outside the prefix is not a call to a FastAPI handler the way the
    other two delegate, because there is no handler for "nothing was
    registered" to call: that is `ServerErrorMiddleware`'s own default response,
    reproduced here rather than invoked. A bare re-raise would seem the more
    direct mirror of it, but `ServerErrorMiddleware` only sends that default
    when no handler is installed at all; once this one is, raising from inside
    it skips the send entirely and leaves the caller with a body-less,
    header-less 500 instead of the plain-text one it gets today.
    """
    if not _under_the_node_api(request):
        return PlainTextResponse("Internal Server Error", status_code=500)
    return JSONResponse(_body("internal"), status_code=500)


def _status_slug(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    except ValueError:
        return "error"


def install_error_handlers(app: FastAPI) -> None:
    """Put the taxonomy in front of the framework responses that escape it.

    Registered on the application because an APIRouter cannot carry an exception
    handler: by the time one runs, the router that would have scoped it is no
    longer in the picture. All three handlers therefore delegate to, or
    reproduce, the framework's own behaviour for anything outside the prefix.
    """
    app.add_exception_handler(RequestValidationError, _validation_refusal)
    app.add_exception_handler(StarletteHTTPException, _http_refusal)
    app.add_exception_handler(Exception, _server_error)
