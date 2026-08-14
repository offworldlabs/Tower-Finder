import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.nodes import Node, NodeConfig, NodeToken

CONFIG_DEFAULTS = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "CRYSTAL_PALACE",
    "fc_hz": 5.7e8,
    "fs_hz": 2.0e6,
    "beam_width_deg": 60.0,
    "beam_azimuth_deg": 45.0,
    "max_range_km": 150.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 2.0,
    "doppler_tolerance_hz": 5.0,
}


def _config(node_id: str, version: int, **overrides) -> NodeConfig:
    return NodeConfig(node_id=node_id, version=version, **(CONFIG_DEFAULTS | overrides))


async def _seed(session, node_id: str) -> Node:
    node = Node(node_id=node_id, node_ref=f"nde{node_id[3:]}00", board_model="raspberrypi5-4gb")
    session.add(node)
    await session.flush()
    return node


async def test_a_node_round_trips(node_session):
    await _seed(node_session, "ret1a2b3c4d")
    await node_session.commit()

    found = (await node_session.execute(select(Node).where(Node.node_id == "ret1a2b3c4d"))).scalar_one()
    assert found.active_config_version == 0
    assert found.publication == "public"
    assert found.status == "active"
    assert found.last_seen_at is None


async def test_config_versions_are_unique_per_node(node_session):
    await _seed(node_session, "ret1a2b3c4d")
    node_session.add_all([_config("ret1a2b3c4d", 1), _config("ret1a2b3c4d", 1)])
    with pytest.raises(IntegrityError):
        await node_session.commit()


async def test_the_same_version_under_two_nodes_is_fine(node_session):
    await _seed(node_session, "ret1a2b3c4d")
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add_all([_config("ret1a2b3c4d", 1), _config("ret9f8e7d6c", 1)])
    await node_session.commit()

    rows = (await node_session.execute(select(NodeConfig))).scalars().all()
    assert len(rows) == 2


async def test_beam_width_null_survives_a_round_trip(node_session):
    """Nullable since contract 1.1.1, and the column has to hold it: no owner is
    asked for their antenna's geometry, so a NOT NULL here failed every real
    node's registration on an IntegrityError behind a 500.
    """
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add(_config("ret9f8e7d6c", 1, beam_width_deg=None))
    await node_session.commit()

    found = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == "ret9f8e7d6c"))).scalar_one()
    assert found.beam_width_deg is None


async def test_beam_azimuth_null_survives_a_round_trip(node_session):
    """null is broadside, 0.0 is aimed due north.

    Conflating them silently re-aims every omnidirectional node in the fleet,
    which is most of it, and the geometry then goes quietly wrong rather than
    failing.
    """
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add(_config("ret9f8e7d6c", 1, beam_azimuth_deg=None))
    await node_session.commit()

    found = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == "ret9f8e7d6c"))).scalar_one()
    assert found.beam_azimuth_deg is None


async def test_a_zero_beam_azimuth_is_kept_as_zero(node_session):
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add(_config("ret9f8e7d6c", 1, beam_azimuth_deg=0.0))
    await node_session.commit()

    found = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == "ret9f8e7d6c"))).scalar_one()
    assert found.beam_azimuth_deg == 0.0


async def test_the_processing_parameters_round_trip(node_session):
    """cpi_s and the two tolerances arrive on the wire at contract 1.1.0 and are
    what the solver needs to interpret a detection's delay and Doppler."""
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add(_config("ret9f8e7d6c", 1, cpi_s=0.25, delay_tolerance_us=1.5, doppler_tolerance_hz=3.25))
    await node_session.commit()

    found = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == "ret9f8e7d6c"))).scalar_one()
    assert found.cpi_s == 0.25
    assert found.delay_tolerance_us == 1.5
    assert found.doppler_tolerance_hz == 3.25


@pytest.mark.parametrize("field", ["cpi_s", "delay_tolerance_us", "doppler_tolerance_hz"])
async def test_a_config_missing_a_processing_parameter_is_rejected(node_session, field):
    """All three are required on the wire, so a row without one is a bug upstream
    rather than a node that legitimately has no CPI. The column says so."""
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add(_config("ret9f8e7d6c", 1, **{field: None}))
    with pytest.raises(IntegrityError):
        await node_session.commit()


async def test_a_token_hash_is_unique(node_session):
    await _seed(node_session, "ret1a2b3c4d")
    await _seed(node_session, "ret9f8e7d6c")
    node_session.add_all(
        [
            NodeToken(node_id="ret1a2b3c4d", token_hash="a" * 64),
            NodeToken(node_id="ret9f8e7d6c", token_hash="a" * 64),
        ]
    )
    with pytest.raises(IntegrityError):
        await node_session.commit()


async def test_a_node_ref_is_unique(node_session):
    await _seed(node_session, "ret1a2b3c4d")
    node_session.add(Node(node_id="ret9f8e7d6c", node_ref="nde1a2b3c4d00"))
    with pytest.raises(IntegrityError):
        await node_session.commit()


async def test_a_config_for_an_unknown_node_is_rejected(node_session):
    """PRAGMA foreign_keys=ON is set on every connection in core/users.py."""
    node_session.add(_config("retnosuchnd", 1))
    with pytest.raises(IntegrityError):
        await node_session.commit()
