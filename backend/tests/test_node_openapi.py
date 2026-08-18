"""The generated node contract, and the properties that make it worth trusting.

`contracts/nodes-v1.openapi.yaml` is the wire contract: the node client and the
conformance harness are independent implementations of it. It is generated
rather than authored, so what wants testing is not its content but that it
still says what the server does — that nothing is published which no request can
reach, and that nothing the server enforces is quietly dropped on the way out.

The first test here is the same assertion CI makes. It is duplicated
deliberately: finding a stale contract at `pytest` time is a regeneration, and
finding it in CI is a round trip.
"""

import pytest
import yaml

from routes.nodes import NODE_API_SERVERS, NODE_API_VERSION
from scripts.generate_openapi import CONTRACT_PATH, contract, render

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

# The four the frozen 1.1.1 published. A generated client turns these into method
# names, so they are as much part of the contract as any field.
OPERATION_IDS = {
    ("/v1/nodes/register", "post"): "registerNode",
    ("/v1/nodes/detection", "post"): "postDetection",
    ("/v1/nodes/heartbeat", "post"): "postHeartbeat",
    ("/v1/nodes/config", "put"): "putConfig",
}

X_RETRY_VALUES = {"never", "retry-after", "backoff"}


@pytest.fixture(scope="module")
def document():
    return contract()


def _operations(document):
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            yield path, method, operation


def _error_responses(document):
    for path, method, operation in _operations(document):
        for status, response in operation["responses"].items():
            if not status.startswith("2"):
                yield f"{method.upper()} {path} {status}", response


def _pydantic_bound_leaks(node, path=""):
    """Every `ge`/`le` key reachable from `node`, each paired with the schema
    path that carries it.

    Walks dicts and lists rather than matching the rendered text against a
    fixed indent, so a leak inside a nested branch (an `anyOf` alternative,
    say) is found at whatever depth it sits at rather than only where a flat
    string match happens to look.
    """
    if isinstance(node, dict):
        for key in ("ge", "le"):
            if key in node:
                yield f"{path}.{key}" if path else key
        for name, value in node.items():
            yield from _pydantic_bound_leaks(value, f"{path}.{name}" if path else name)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _pydantic_bound_leaks(item, f"{path}[{index}]")


# ── the gate ─────────────────────────────────────────────────────────────────


def test_the_committed_contract_is_what_the_routes_generate(document):
    """The whole of what makes generated-as-truth safe.

    Without this the committed file could be hand-edited to match a change
    instead of the change being noticed, which is the failure the hand-written
    contract had and the reason it was retired.
    """
    assert CONTRACT_PATH.read_text() == render(document), (
        f"{CONTRACT_PATH} is stale. Regenerate it in this commit: cd backend && python -m scripts.generate_openapi"
    )


def test_the_contract_carries_the_node_apis_own_version(document):
    assert document["info"]["version"] == NODE_API_VERSION


def test_it_describes_the_four_node_endpoints_and_nothing_else(document):
    assert {(path, method) for path, method, _ in _operations(document)} == set(OPERATION_IDS)


def test_the_servers_are_origins_and_the_paths_carry_the_version(document):
    """Joined, these have to be the URL a node already posts to.

    The frozen contract expressed the same address the other way round, as a
    `.../v1` server and a `/nodes/...` path, so getting this wrong would move
    every endpoint by a path segment while every part of it still looked right.
    """
    assert document["servers"] == NODE_API_SERVERS
    assert [server["url"] for server in document["servers"]] == [
        "https://api.retina.fm",
        "https://staging-api.retina.fm",
    ]
    assert all(path.startswith("/v1/nodes/") for path in document["paths"])


# ── nothing published that cannot happen ─────────────────────────────────────


def test_no_operation_publishes_a_422(document):
    """FastAPI declares one automatically wherever a request model exists, and
    under this prefix none can reach the wire: the taxonomy handler converts
    every RequestValidationError into a 400. The paired half is below."""
    published = [name for name, _ in _error_responses(document) if name.endswith(" 422")]

    assert published == []


def test_and_a_malformed_body_really_does_answer_400(node_client, registered_node):
    """The other half. Dropping the 422 from the document would be a lie if the
    server could still send one, so the two assertions travel together."""
    token, _node_id = registered_node

    response = node_client.post(
        "/v1/nodes/detection",
        json={**FRAME, "seq": -1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


# ── nothing enforced that is not published ───────────────────────────────────


def test_no_bound_is_published_as_a_pydantic_keyword(document):
    """`ge` and `le` are not JSON Schema, so a consumer drops them silently.

    They appear when a constraint is applied over a type that already carries a
    validator, which is easy to reintroduce and invisible in review: the field
    still validates, and only the published bound goes missing. Walked over the
    parsed document rather than matched against the rendered file, so a leak at
    any nesting depth is caught regardless of how many spaces it happens to be
    indented under, and the failure names the schema path that carries it.
    """
    leaks = sorted(_pydantic_bound_leaks(document))

    assert leaks == [], f"pydantic bound keyword(s) reached the contract: {', '.join(leaks)}"


def test_every_numeric_bound_the_models_enforce_reaches_the_schema(document):
    schemas = document["components"]["schemas"]

    assert schemas["DetectionFrame"]["properties"]["config_version"]["minimum"] == 1
    cpu_pct = schemas["NodeHealth"]["properties"]["cpu_pct"]["anyOf"][0]
    assert (cpu_pct["minimum"], cpu_pct["maximum"]) == (0, 100)


def test_the_timestamps_are_still_typed_as_datetimes(document):
    """A custom serialiser erases `format` unless it is put back, and this is the
    one field a node uses to measure its own clock offset."""
    schemas = document["components"]["schemas"]

    for model in ("RegisterResponse", "HeartbeatResponse"):
        assert schemas[model]["properties"]["server_time"]["format"] == "date-time"


# ── the credential ───────────────────────────────────────────────────────────


def test_the_bearer_scheme_is_published(document):
    """Without it a generated client sends no credential at all, and the three
    authenticated endpoints look open."""
    assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"


def test_every_endpoint_but_registration_requires_the_bearer(document):
    requires = {(path, method) for path, method, operation in _operations(document) if operation.get("security")}

    assert requires == set(OPERATION_IDS) - {("/v1/nodes/register", "post")}


# ── behaviour the schema cannot carry on its own ─────────────────────────────


def test_every_refusal_is_annotated_with_what_the_node_may_do(document):
    """The point of the `x-` vocabulary: a client acts on a field rather than on
    someone's reading of a paragraph, so a response without one is a response a
    node has to guess at."""
    for name, response in _error_responses(document):
        assert response.get("x-retry") in X_RETRY_VALUES, name
        assert isinstance(response.get("x-terminal"), bool), name


def test_only_a_refused_credential_is_terminal(document):
    """`x-terminal` means an operator has to act, and a node that keeps retrying
    is making things worse. If anything else acquires it, the vocabulary in the
    application description needs rewriting rather than this test relaxing."""
    terminal = {name for name, response in _error_responses(document) if response["x-terminal"]}

    assert terminal == {
        "PUT /v1/nodes/config 401",
        "POST /v1/nodes/detection 401",
        "POST /v1/nodes/heartbeat 401",
    }


def test_every_retry_after_response_publishes_the_header(document):
    """A node told to honour `Retry-After` needs the header to be part of the
    contract rather than something it discovers."""
    for name, response in _error_responses(document):
        if response["x-retry"] == "retry-after":
            assert response["headers"]["Retry-After"]["required"] is True, name


def test_every_refusal_carries_the_contracts_error_shape(document):
    for name, response in _error_responses(document):
        schema = response["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ErrorBody"}, name


def test_the_operation_ids_are_the_ones_the_node_client_was_built_against(document):
    published = {(path, method): operation["operationId"] for path, method, operation in _operations(document)}

    assert published == OPERATION_IDS


# ── the document itself ──────────────────────────────────────────────────────


def test_the_committed_file_parses_as_yaml_and_matches_the_document(document):
    assert yaml.safe_load(CONTRACT_PATH.read_text()) == document


# Substrings that mark a description as written for whoever changes this
# module rather than for a node author: internal symbols a handler's own
# docstring names, and internal reasoning a model's own docstring names when
# neither supplies an explicit `description=`. Real leaks caught here, not a
# hypothetical list: the first four are handler docstrings that used to reach
# operations before they got explicit descriptions, and the last two are
# ErrorBody's docstring before it was trimmed to what a node author needs.
_MAINTAINER_LEAKS = (
    "state.connected_nodes",
    "_file_frame",
    "services/node_config.py",
    "uvicorn",
    "exclude_none",
    "call site",
)


def test_the_descriptions_are_written_for_a_node_author(document):
    """Neither an operation's `description` nor a component schema's may carry
    prose written for whoever changes this module rather than for whoever
    implements a node against it.

    FastAPI falls an operation back to its handler's docstring, and Pydantic
    falls a schema back to its model's docstring, whenever neither supplies an
    explicit description. Both fallbacks are written for a maintainer, so this
    pins that neither leaked through into what gets published.
    """
    for path, method, operation in _operations(document):
        description = operation["description"]
        for leak in _MAINTAINER_LEAKS:
            assert leak not in description, f"{method.upper()} {path} publishes {leak}"

    for name, schema in document["components"]["schemas"].items():
        description = schema.get("description", "")
        for leak in _MAINTAINER_LEAKS:
            assert leak not in description, f"schema {name} publishes {leak}"
