"""Router wiring and per-path body caps for the v1 node API.

No handlers exist yet, so these tests assert on the middleware and the router
skeleton rather than on any response payload. They are written to survive the
handlers landing: see the `!= 413` note below before changing an assertion.
"""

import pytest
from fastapi.testclient import TestClient

import main
from routes.nodes import NODE_BODY_LIMITS

# The lifespan starts background workers this file has no use for, so the client
# is built without the context-manager form that would run it.
client = TestClient(main.app, raise_server_exceptions=False)

# The method each path answers to, from the wire contract: config is a PUT, the
# rest are POSTs. Sending the wrong verb would still exercise the middleware,
# but it would stop doing so the moment a handler landed.
NODE_METHODS = {
    "/v1/nodes/register": "POST",
    "/v1/nodes/config": "PUT",
    "/v1/nodes/heartbeat": "POST",
    "/v1/nodes/detection": "POST",
}

KIB = 1024


def _send_to(path: str, method: str, body: bytes):
    return client.request(method, path, content=body, headers={"Content-Type": "application/json"})


def _send(path: str, body: bytes):
    return _send_to(path, NODE_METHODS[path], body)


@pytest.mark.parametrize("path", sorted(NODE_METHODS))
def test_one_byte_over_the_cap_is_refused(path):
    """A body over the path's own cap gets 413 in the node error taxonomy."""
    r = _send(path, b"x" * (NODE_BODY_LIMITS[path] + 1))
    assert r.status_code == 413
    assert r.json() == {"error": "too_large"}


@pytest.mark.parametrize("path", sorted(NODE_METHODS))
def test_a_body_at_the_cap_is_not_refused(path):
    """The largest permitted body passes the cap.

    Asserted as `!= 413` rather than a status: with no handlers mounted these
    paths answer 404, and they will answer 2xx once the handlers land. The point
    of the test is that the middleware let the request through, so leave the
    assertion alone rather than tightening it to whatever code you now see.
    """
    r = _send(path, b"x" * NODE_BODY_LIMITS[path])
    assert r.status_code != 413


@pytest.mark.parametrize("path", sorted(NODE_METHODS))
def test_a_trailing_slash_does_not_escape_the_cap(path):
    """`/v1/nodes/register/` is capped the same as `/v1/nodes/register`.

    The middleware runs ahead of routing, so it never sees the path Starlette
    would canonicalise. Matching the raw path alone let a caller opt out of an
    8 KiB cap into the 5 MB global one by adding a character.
    """
    r = _send_to(f"{path}/", NODE_METHODS[path], b"x" * (NODE_BODY_LIMITS[path] + 1))
    assert r.status_code == 413
    assert r.json() == {"error": "too_large"}


def test_detection_cap_is_larger_than_the_other_three():
    """The 64 KiB detection cap is real, not a copy-paste of 8 KiB.

    Same `!= 413` reasoning as above.
    """
    assert NODE_BODY_LIMITS["/v1/nodes/detection"] == 64 * KIB
    assert set(NODE_BODY_LIMITS.values()) == {8 * KIB, 64 * KIB}
    r = _send("/v1/nodes/detection", b"x" * (8 * KIB + 1))
    assert r.status_code != 413


def test_a_non_node_path_is_not_capped_at_8_kib():
    """The per-path table governs only the paths in it.

    Same `!= 413` reasoning as above.
    """
    r = client.post(
        "/api/radar/detections",
        content=b"x" * (8 * KIB + 1),
        headers={"Content-Type": "application/json", "X-API-Key": "test-key-abc123"},
    )
    assert r.status_code != 413


def test_a_non_node_path_over_the_global_cap_keeps_the_detail_shape():
    """The two refusal shapes stay distinct.

    Node paths answer the taxonomy's `{"error": ...}`; everything else keeps the
    pre-existing `{"detail": ...}`. Folding the two branches into one would
    change a shape callers already parse, so this test pins them apart.
    """
    r = client.post(
        "/api/radar/detections",
        content=b"x" * 100,  # small body, oversized declaration
        headers={
            "Content-Length": str(10 * 1024 * 1024),
            "Content-Type": "application/json",
            "X-API-Key": "test-key-abc123",
        },
    )
    assert r.status_code == 413
    body = r.json()
    assert "error" not in body
    assert str(main._MAX_BODY_BYTES) in body["detail"]


def test_the_node_router_is_mounted_under_v1_nodes():
    """The prefix is right and main mounts that exact router object.

    `include_router` copies routes at call time, so including an empty router
    leaves no trace in `app.routes`. Until the first handler lands there is
    nothing structural to assert, and this check is correspondingly weak: it
    confirms the prefix and that main imported the same object it mounts, not
    that the mount call ran. Once a handler exists, assert on `app.routes`.
    """
    from routes import nodes

    assert nodes.router.prefix == "/v1/nodes"
    assert main.nodes_router is nodes.router


def test_the_skeleton_carries_all_three_endpoint_modules():
    """Each endpoint module is wired into the parent router.

    The includes are what the skeleton exists for, and while every router is
    still empty they leave no evidence behind: dropping all three would not
    fail a single assertion above. Identity against each module's own router
    is the available guard, and it catches the realistic regression of a
    module being renamed or its import dropped. It does not prove
    `include_router` ran, so add that assertion with the first handler.
    """
    from routes import node_config, node_register, node_stream, nodes

    assert nodes.node_register_router is node_register.router
    assert nodes.node_config_router is node_config.router
    assert nodes.node_stream_router is node_stream.router


def test_main_wires_the_node_caps_into_the_middleware():
    """The cap table reaches the middleware rather than sitting unused."""
    limits = [m.kwargs.get("limits") for m in main.app.user_middleware if m.cls is main.LimitUploadSize]
    assert limits == [NODE_BODY_LIMITS]
