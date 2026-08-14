"""A node that registers without a position must join no overlap pairs.

`POST /api/radar/detections` has no config to offer for a node it has not seen
before, so it registers one with `{"node_id": ...}` and nothing else. Left to
default, that geometry is (0, 0): every such node lands on one footprint in the
Gulf of Guinea and overlaps every other one completely. The neighbour graph goes
complete, the associator fuses a dozen unrelated nodes into a single candidate,
and the solver queue backs up behind candidates that are pure artefact
(86cb5hef4).

The guard itself lives in retina-analytics (`_has_receiver_position`), so what
is pinned here is the behaviour this repo depends on rather than its
implementation: nothing but the submodule revision stands between main and a
repeat, and the failure mode is a saturated solver rather than an exception.
"""

import httpx
import pytest

from core import state
from main import app

HEADERS_OK = {"X-API-Key": "test-key-abc123"}

# Enough that a complete graph is unmistakable: 10 nodes would pair into 45
# zones, against the 0 a positionless fleet should produce.
_N = 10
_IDS = [f"test-unpositioned-{i}" for i in range(_N)]

# Greenville, far enough apart to be a real fleet and close enough to overlap.
_POSITIONED = [
    {
        "node_id": f"test-positioned-{i}",
        "rx_lat": 34.85 + i * 0.03,
        "rx_lon": -82.39 + i * 0.03,
        "tx_lat": 34.90,
        "tx_lon": -82.45,
        "fc_hz": 195e6,
    }
    for i in range(3)
]


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_nodes():
    yield
    for node_id in list(state.connected_nodes):
        if node_id.startswith("test-"):
            state.connected_nodes.pop(node_id, None)
            state.node_associator.unregister_node(node_id)
            state.node_analytics.retire_node(node_id)


def _zones_naming(node_ids) -> int:
    wanted = set(node_ids)
    return sum(1 for pair in state.node_associator.overlap_zones if wanted & set(pair))


class TestUnpositionedNodesStayOutOfTheGraph:
    async def test_a_fleet_registered_over_http_builds_no_overlap_zones(self, client):
        for node_id in _IDS:
            r = await client.post("/api/radar/detections", headers=HEADERS_OK, json={"node_id": node_id})
            assert r.status_code == 200

        assert _zones_naming(_IDS) == 0
        assert all(state.node_associator._neighbors.get(n, set()) == set() for n in _IDS)

    async def test_the_nodes_are_still_registered(self, client):
        """Declining the pairing must not decline the node."""
        await client.post("/api/radar/detections", headers=HEADERS_OK, json={"node_id": _IDS[0]})

        assert _IDS[0] in state.connected_nodes
        assert _IDS[0] in state.node_associator.node_geometries

    async def test_a_positioned_fleet_still_pairs(self, client):
        """The guard has to read absence, not treat every node as absent."""
        await client.post(
            "/api/radar/detections/bulk",
            headers=HEADERS_OK,
            json={"nodes": [{"node_id": e["node_id"], "config": e, "frames": []} for e in _POSITIONED]},
        )

        # All three pair with each other, so all three pairs must appear. A bare
        # `> 0` would also pass with a guard that wrongly excluded two of the
        # three, which is the failure this control test exists to catch.
        assert _zones_naming(e["node_id"] for e in _POSITIONED) == 3
