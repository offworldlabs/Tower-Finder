"""Every refusal under /v1/nodes wears the contract's `Error` shape.

The node client parses `body["error"]`, so a response carrying FastAPI's
`{"detail": ...}` instead falls into its unknown-error path. That is worst on
the case the shape exists for: a revoked token, where the contract has the node
stop streaming, keep heartbeating and deliberately not re-register.

Two framework responses were reaching the wire unconverted, and they are tested
together because one handler pair fixes both: the 401 `bearer_node` raises, and
the 422 a request model raises before any handler runs.

The scoping is as load bearing as the shape. The rest of the API is not written
to this taxonomy and its callers parse FastAPI's shape, so the last test here
pins that a non-node path is left alone.
"""

import pytest

FRAME = {
    "t": 1753900000.123,
    "seq": 0,
    "boot_id": "k3n8v2qp71ab",
    "config_version": 1,
    "delay": [],
    "doppler": [],
    "snr": [],
    "adsb_hex": [],
}
BEAT = {"state": "streaming", "uptime_s": 1, "boot_id": "k3n8v2qp71ab", "config_version": 1}

# The three bearer-authenticated paths, with a body each handler would accept if
# the credential resolved. A body that would itself be refused would not tell us
# which of the two refusals we were looking at.
AUTHENTICATED = [
    ("POST", "/v1/nodes/detection", FRAME),
    ("POST", "/v1/nodes/heartbeat", BEAT),
    ("PUT", "/v1/nodes/config", {}),
]


def _register_body(**overrides):
    """A registration body that is well formed until an override breaks it."""
    accepted = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
    body = {
        "node_id": "ret1a2b3c4d",
        "board_model": "pi5-v3-arm64",
        "agreements": {
            "licence": accepted,
            "remote_management": accepted,
            "publication": {**accepted, "choice": "public"},
        },
        "config": {},
    }
    body.update(overrides)
    return body


# ── 401 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "path", "body"), AUTHENTICATED)
def test_a_bad_token_is_refused_in_the_taxonomy(node_client, registered_node, method, path, body):
    r = node_client.request(method, path, json=body, headers={"Authorization": "Bearer not-a-token"})

    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(("method", "path", "body"), AUTHENTICATED)
def test_no_credential_at_all_is_refused_the_same_way(node_client, registered_node, method, path, body):
    """A missing header and a dead token are one refusal, as they were before.

    Distinguishing them would say whether the token presented was ever a token,
    which is the sort of thing the rest of this API's refusals are careful not
    to say.
    """
    r = node_client.request(method, path, json=body)

    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


# ── 422, which the contract does not have ────────────────────────────────────


def test_a_malformed_frame_is_a_400_naming_the_field(node_client, registered_node):
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        json={**FRAME, "seq": -1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body", "detail": "seq"}


def test_a_frame_naming_a_node_is_refused_by_the_key_it_added(node_client, registered_node):
    """`extra="forbid"` is load bearing on this model, so its refusal is too."""
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        json={**FRAME, "node_id": "ret00000000"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body", "detail": "node_id"}


def test_an_unknown_node_state_is_a_400(node_client, registered_node):
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/heartbeat",
        json={**BEAT, "state": "wat"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body", "detail": "state"}


def test_a_malformed_agreements_block_answers_like_a_bad_config(node_client):
    """The half of the contract's 400 sentence that used to answer 422.

    `POST /nodes/register`'s 400 covers "configuration **or agreement records**
    failed validation". Configuration validation runs inside the handler and
    answered 400; a malformed `agreements` block never reached the handler and
    answered 422, so one sentence had two answers.
    """
    body = _register_body(
        agreements={
            "licence": {"version": "2026-07-01"},
            "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
            "publication": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z", "choice": "public"},
        }
    )

    r = node_client.post("/v1/nodes/register", json=body)

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body", "detail": "agreements.licence.accepted_at"}


def test_the_field_path_carries_an_array_index(node_client, registered_node):
    """`delay.1` rather than `delay`, so a node can find the element it sent."""
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        json={
            **FRAME,
            "delay": [1.0, "not a number"],
            "doppler": [1.0, 2.0],
            "snr": [1.0, 2.0],
            "adsb_hex": [None, None],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body", "detail": "delay.1"}


def test_the_refusal_names_the_field_and_nothing_else(node_client, registered_node):
    """No echo of what the caller sent, and no pydantic documentation URL.

    The registration handler's refusals are opaque so that a caller cannot learn
    which node identities exist, and a validation error that named more than a
    field would be the one place that leaked. It also keeps the body inside
    `Error.detail`'s 512-character bound without depending on what was sent.
    """
    r = node_client.post("/v1/nodes/register", json=_register_body(node_id="../../etc/passwd"))

    assert r.status_code == 400
    body = r.json()
    assert body == {"error": "invalid_body", "detail": "node_id"}
    assert "passwd" not in r.text
    assert "pydantic" not in r.text


def test_a_long_field_path_is_truncated_to_the_contracts_bound(node_client, registered_node):
    """An unknown key is caller-supplied, and `Error.detail` is capped at 512.

    Same reasoning as the registration handler's own truncation: a key longer
    than the bound would fail `ErrorBody`'s validation inside the handler and
    turn a 400 into a 500, which is the one response a node retries.
    """
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        json={**FRAME, "x" * 900: 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 400
    assert len(r.json()["detail"]) == 512


def test_a_body_that_is_not_json_at_all_is_a_400(node_client, registered_node):
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        content=b"{",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_body"


def test_a_body_that_cannot_even_be_read_is_a_400_in_the_taxonomy(node_client, registered_node, monkeypatch):
    """FastAPI answers a body-read failure that is not a JSON decode error with
    a bare `HTTPException(400, detail="There was an error parsing the body")`
    (fastapi.routing.get_request_handler's catch-all around the body read).
    That detail has spaces and capitals, so it never matches `_SLUG`, and
    without the taxonomy's special case it would fall back to `bad_request`,
    a slug `API_DESCRIPTION` never documents for 400.

    A genuine transport-level read failure (the client disconnecting
    mid-upload) is what triggers this in production, and TestClient's
    synchronous ASGI transport gives no way to provoke that from the wire.
    `Request.body` is patched instead, for this one request only, to raise the
    same shape of exception FastAPI's own `except Exception` catches: anything
    that is not a `json.JSONDecodeError` and not an `HTTPException`.
    """
    from starlette.requests import Request

    async def _unreadable(self):
        raise RuntimeError("simulated transport read failure")

    monkeypatch.setattr(Request, "body", _unreadable)
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        json={},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_body"}


# ── the refusals that were already right ─────────────────────────────────────


def test_an_invalid_configuration_still_says_so(node_client, registered_node):
    """`invalid_config` stays the handler's slug, distinct from `invalid_body`.

    The distinction is the point: retina-gui shows a configuration error next to
    the input it belongs to, and a node reading `invalid_config` on a malformed
    detection frame would resend a configuration that was never the problem.
    """
    token, _node_id = registered_node

    r = node_client.put("/v1/nodes/config", json={"rx_lat": 999}, headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_config"


def test_an_oversized_body_is_untouched(node_client, registered_node):
    """The body-cap middleware already emits the taxonomy, and runs ahead of routing."""
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/detection",
        content=b"x" * (64 * 1024 + 1),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert r.json() == {"error": "too_large"}


def test_an_unrouted_path_under_the_prefix_is_in_the_taxonomy(node_client):
    """Starlette raises its own HTTPException for these, with a prose detail.

    A node that mistypes a path, or holds an older one, gets `{"error": ...}`
    like everything else under the prefix rather than `{"detail": "Not Found"}`.
    """
    r = node_client.post("/v1/nodes/nonesuch", json={})

    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}


# ── 5XX ──────────────────────────────────────────────────────────────────────


def test_an_unhandled_exception_answers_500_in_the_taxonomy(node_client, registered_node, monkeypatch):
    """The contract publishes `Error` for 5XX; the server has to send it.

    `_file_frame` is the last thing `post_detection` calls before returning, so
    raising from it stands in for any unhandled exception a node handler could
    raise: nothing upstream of it is an `HTTPException` or a validation error,
    so neither of the other two handlers would ever see this.
    """
    import routes.node_stream

    def _boom(node_id, frame):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(routes.node_stream, "_file_frame", _boom)
    token, _node_id = registered_node

    r = node_client.post("/v1/nodes/detection", json=FRAME, headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 500
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"error": "internal"}


def test_the_500_names_no_detail_and_leaks_no_exception_text(node_client, registered_node, monkeypatch):
    """An exception's text is server-internal and must not reach a node."""
    import routes.node_stream

    def _boom(node_id, frame):
        raise RuntimeError("a message a node must never see")

    monkeypatch.setattr(routes.node_stream, "_file_frame", _boom)
    token, _node_id = registered_node

    r = node_client.post("/v1/nodes/detection", json=FRAME, headers={"Authorization": f"Bearer {token}"})

    assert "detail" not in r.json()
    assert "a message a node must never see" not in r.text


# ── scoping ──────────────────────────────────────────────────────────────────


def test_a_path_that_merely_starts_with_the_prefix_is_not_under_it(node_client):
    """`/v1/nodesx` is a different API, and the scoping check has to say so.

    The predicate is an exact match or a `/`-bounded child, and a bare
    `startswith` would claim this path as well. Nothing routes here today, so the
    only thing standing between that mistake and a silent change of contract for
    some future sibling endpoint is this test.
    """
    r = node_client.post("/v1/nodesx", json={})

    assert r.status_code == 404
    assert "detail" in r.json()
    assert "error" not in r.json()


def test_the_rest_of_the_api_keeps_fastapis_shape(node_client):
    """The taxonomy is the node API's, not the application's.

    Everything else here is read by the frontend and the dashboard, which parse
    `detail`, so a global handler would have been a breaking change to callers
    that never asked for this contract.
    """
    r = node_client.get("/api/towers")

    assert r.status_code == 422
    assert "detail" in r.json()
    assert "error" not in r.json()


def test_an_unhandled_exception_outside_the_prefix_keeps_the_plain_text_500(node_client, monkeypatch):
    """The 5XX handler is scoped like the other two: everywhere else is untouched.

    `GET /api/config` reads `_CONFIG_PATH` off disk with no scoping of its own;
    pointing it at a file that cannot exist raises `FileNotFoundError` before the
    handler produces anything, which is a genuine unhandled exception rather
    than one raised on purpose for the test.
    """
    import routes.towers

    monkeypatch.setattr(routes.towers, "_CONFIG_PATH", "/nonexistent/does-not-exist.json")

    r = node_client.get("/api/config")

    assert r.status_code == 500
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.text == "Internal Server Error"
