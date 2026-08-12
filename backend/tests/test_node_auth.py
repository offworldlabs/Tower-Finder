import hashlib
import re

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select

from core.nodes import NodeToken
from services.node_auth import bearer_node, mint_node_ref, mint_token, revoke_tokens


def _request_raw(header: str) -> Request:
    """A Request carrying one Authorization header and nothing else.

    Built from a scope rather than through the app, because bearer_node is a
    dependency and these tests are about the dependency, not the routes.
    """
    headers = [(b"authorization", header.encode())] if header else []
    return Request({"type": "http", "method": "POST", "path": "/v1/nodes/heartbeat", "headers": headers})


def _request(token: str) -> Request:
    return _request_raw(f"Bearer {token}")


async def _tokens(session, node_id: str) -> list[NodeToken]:
    return list((await session.execute(select(NodeToken).where(NodeToken.node_id == node_id))).scalars().all())


async def test_only_the_hash_is_stored(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    await node_session.commit()
    rows = await _tokens(node_session, "ret1a2b3c4d")
    assert rows[0].token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert all(token not in r.token_hash for r in rows)


async def test_a_minted_token_resolves(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    await node_session.commit()
    assert await bearer_node(_request(token), node_session) == "ret1a2b3c4d"


async def test_a_revoked_token_does_not_resolve(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    await revoke_tokens(node_session, "ret1a2b3c4d", reason="reissue")
    await node_session.commit()
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request(token), node_session)
    assert excinfo.value.status_code == 401


async def test_revoking_reports_how_many_it_revoked(node_session, seeded_node):
    await mint_token(node_session, "ret1a2b3c4d")
    await mint_token(node_session, "ret1a2b3c4d")
    assert await revoke_tokens(node_session, "ret1a2b3c4d", reason="reissue") == 2
    # Already-revoked rows are not revoked twice, so a second call reports nothing
    # and does not overwrite the reason the first one recorded.
    assert await revoke_tokens(node_session, "ret1a2b3c4d", reason="retired") == 0


async def test_an_uncommitted_registration_leaves_no_usable_token(node_session, seeded_node):
    """The flush-not-commit contract: a rolled back registration mints nothing."""
    token = await mint_token(node_session, "ret1a2b3c4d")
    await node_session.rollback()
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request(token), node_session)
    assert excinfo.value.status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer ", "Token xyz"])
async def test_a_malformed_authorization_header_is_401(node_session, header):
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request_raw(header), node_session)
    assert excinfo.value.status_code == 401


async def test_an_unknown_token_is_401(node_session):
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request("nonsense"), node_session)
    assert excinfo.value.status_code == 401


def test_a_minted_node_ref_matches_the_wire_pattern():
    # The contract's own regex, from nodes_api_v1.yml NodeRef, rather than a
    # hand-rolled restatement of it: where the plan and the spec disagree the
    # spec wins, so the test asserts the thing the spec actually says.
    assert re.fullmatch(r"(nde|sim)[0-9a-z]{12}", mint_node_ref())


def test_minting_does_not_repeat():
    assert len({mint_node_ref() for _ in range(1000)}) == 1000
