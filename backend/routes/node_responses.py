"""What the four node endpoints publish for each failure, and the `x-` vocabulary.

The generated OpenAPI schema is the node API's wire contract (86cb2d059). A
schema carries shapes and status codes but not behaviour, and the half a node
acts on is behaviour: whether a refusal may be retried, and whether retrying can
ever help. That half lives here, on the response object it describes, so it
cannot drift from the route that raises it. A separate document saying "never
retry a 403" goes stale the moment something else starts raising 403.

`API_DESCRIPTION` below is the application's OpenAPI description, and so the
published contract's, which is where the vocabulary is defined for a reader.
"""

from typing import Any

from routes.node_schemas import ErrorBody

# `x-retry` values. The question they answer is narrow on purpose: may the node
# send this identical request again, and when.
RETRY_NEVER = "never"
RETRY_AFTER_HEADER = "retry-after"
RETRY_BACKOFF = "backoff"

# Per-path request body caps: 8 KiB for registration, heartbeat and
# configuration, 64 KiB for a detection frame. Published per operation as
# `x-max-body-bytes` (see the four routes' `openapi_extra`) so a node author can
# read the number off the contract rather than discover it by testing. Keys are
# full app paths, since the contract's server URL already carries the `/v1`
# prefix.
#
# Defined here rather than in routes/nodes.py, which is where the taxonomy and
# the router live: the three endpoint modules need this to annotate their own
# routes, and routes/nodes.py imports those modules, so a definition there
# would be a cycle. routes/nodes.py re-exports it for main.py's
# LimitUploadSize middleware.
NODE_BODY_LIMITS: dict[str, int] = {
    "/v1/nodes/register": 8 * 1024,
    "/v1/nodes/config": 8 * 1024,
    "/v1/nodes/heartbeat": 8 * 1024,
    "/v1/nodes/detection": 64 * 1024,
}

API_DESCRIPTION = """\
Tower Finder's HTTP API. The paths under `/v1/nodes` are the RETINA node ingest
contract, generated from the server and versioned as a unit; everything else is
internal to the map, the dashboard and the admin surfaces.

## Error taxonomy

Every refusal under `/v1/nodes` carries `Error`, whose required key is `error`
and whose optional `detail` names a field rather than describing it. The rest of
this API answers in FastAPI's `{"detail": ...}`, which its own callers parse.

| Status | Means |
|---|---|
| `400` | The body was refused. `invalid_config` for a configuration that failed validation, `invalid_body` for one that failed the schema |
| `401` | The bearer token is bad, revoked or expired |
| `403` | Registration refused, without saying why |
| `409` | The frame names a `config_version` this server never issued |
| `413` | The body exceeded the cap for its path |
| `429` | Rate limited |

`403` is deliberately opaque: unknown device, not yet accepted by Mender,
already holding a valid token and in cooldown are one response, at one latency,
with one `Retry-After`. Registration is limited per `node_id` and answers that
same `403` rather than a `429`, since a distinct status would confirm to an
unauthenticated caller that the identity it named is one the server tracks.

A path under the prefix this server does not route, or a routed one called with
the wrong method, answers in this same taxonomy too, as `{"error": "not_found"}`
or `{"error": "method_not_allowed"}` respectively. Neither is a per-operation
response, so neither is declared on any individual operation below.

## Retry semantics: `x-retry` and `x-terminal`

OpenAPI has no vocabulary for either, so these two extensions are invented here
and carried on the response object. A node acts on a field rather than on
someone's reading of a paragraph.

`x-retry` says what may be done with the identical request:

| Value | Meaning |
|---|---|
| `never` | Resending it unchanged cannot succeed. Something has to change first, and the response's description says what |
| `retry-after` | Honour the `Retry-After` header, then retry, backing off with jitter |
| `backoff` | Retry with exponential backoff and jitter |

`x-terminal: true` says the condition will not clear by anything the node can do:
it needs an operator. Only a refused credential is terminal, and the response
that carries it is the one case where continuing to retry makes things worse.

## Body cap: `x-max-body-bytes`

Each operation carries `x-max-body-bytes`: the `Content-Length` ceiling checked
ahead of parsing, past which the request answers `413`. Declared on the
operation rather than the response, since it describes what the operation
accepts rather than how to react to a refusal.
"""


def _response(
    description: str,
    *,
    retry: str,
    terminal: bool = False,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `Error` response, annotated. FastAPI merges unknown keys verbatim."""
    response: dict[str, Any] = {
        "model": ErrorBody,
        "description": description,
        "x-retry": retry,
        "x-terminal": terminal,
    }
    if headers is not None:
        response["headers"] = headers
    return response


RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying. Honour it, then back off with jitter.",
        "required": True,
        "schema": {"type": "integer", "minimum": 0, "maximum": 86400},
    }
}

UNAUTHORIZED = _response(
    "Token bad, revoked or expired. Stop streaming, surface it locally, and keep heartbeating. "
    "Do not re-register: recovering a token needs an operator to reactivate the node, and treating "
    "a 401 as the trigger turns a deliberate revocation into a registration storm.",
    retry=RETRY_NEVER,
    terminal=True,
)

RATE_LIMITED = _response(
    "Rate limited, per token and per endpoint. The skipped frames are best dropped rather than "
    "accumulated: a frame delivered late carries an old timestamp and is rejected by the "
    "association gate rather than paired against whatever is current.",
    retry=RETRY_AFTER_HEADER,
    headers=RETRY_AFTER,
)

TOO_LARGE = _response(
    "The body exceeded this path's cap, which is checked from `Content-Length` before parsing. "
    "Per-field bounds in the schemas do not make this unreachable: `HeartbeatRequest.errors` alone "
    "permits 16 KiB of strings, `RegisterRequest.config` carries no bound at all, and JSON puts no "
    "length limit on a number's text, so a schema-valid body can still exceed it.",
    retry=RETRY_NEVER,
)

SERVER_ERROR = _response(
    "Server side, though a 502 or 504 is the edge's own response rather than the origin's, and carries "
    "whatever body that gateway sends instead of this `Error`. Abandon the request and retry, backing "
    "off with jitter if it persists: on detection and heartbeat that retry is the node's next frame or "
    "beat rather than a resend of this one, while registration and configuration retry the identical body.",
    retry=RETRY_BACKOFF,
)


def _refused_body(what: str) -> dict[str, Any]:
    """The 400 a body earns, whichever half of it failed.

    Two slugs rather than one, because they are different faults and a node
    should be able to tell them apart: one told `invalid_config` by a
    mis-serialised frame would resend a configuration that was never at fault.
    """
    return _response(
        f"{what} `detail` names the offending field and nothing else, so retina-gui can put the "
        "message next to the input it belongs to. Retrying unchanged will not help.",
        retry=RETRY_NEVER,
    )


INVALID_REGISTRATION = _refused_body(
    "The configuration or the agreement records failed validation: `invalid_config` for a "
    "configuration value out of range, `invalid_body` for anything that failed the schema."
)

INVALID_CONFIG = _refused_body(
    "The configuration failed validation, as `invalid_config`. A body that is not JSON at all "
    "lands here too, since the remedy is the same."
)

INVALID_FRAME = _refused_body("The frame failed the schema, as `invalid_body`.")

INVALID_BEAT = _refused_body("The heartbeat failed the schema, as `invalid_body`.")

REGISTRATION_REFUSED = _response(
    "Refused, without saying why. Unknown device, not yet accepted by Mender, already holding a "
    "valid token, and an identity in cooldown are one response, at one latency, with one "
    "`Retry-After`. It is also the normal answer while Mender acceptance is pending, and the "
    "answer to a node registering too often, so a node that has just been flashed should expect "
    "it and keep retrying.",
    retry=RETRY_AFTER_HEADER,
    headers=RETRY_AFTER,
)

UNKNOWN_CONFIG_VERSION = _response(
    "Unknown `config_version`: this server never issued it, so the frame cannot be interpreted. "
    "`PUT /v1/nodes/config`, adopt the version that comes back, then resume. A version the server "
    "issued and has since replaced is not this, and those frames are accepted with `config_stale` "
    "set instead.",
    retry=RETRY_NEVER,
)
