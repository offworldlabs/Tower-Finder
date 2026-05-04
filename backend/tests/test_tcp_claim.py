"""End-to-end tests for the TCP claim code protocol (CLAIM_ACK / CLAIM_NACK).

Claim flow:
  Node sends HELLO with claim_code
    → valid code, unowned node   → CLAIM_ACK {user_id}
    → invalid / expired code     → CLAIM_NACK {error}
    → valid code, already owned  → CLAIM_ACK {note: "already_owned"}, no user_id
"""

import asyncio
import json
import time

import pytest

from services.tcp_handler import handle_tcp_client


_NODE_ID = "claim-test-node"


def _msg(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8") + b"\n"


def _hello(node_id: str = _NODE_ID, claim_code: str | None = None) -> bytes:
    m = {"type": "HELLO", "node_id": node_id, "version": "1.0", "is_synthetic": True}
    if claim_code is not None:
        m["claim_code"] = claim_code
    return _msg(m)


class _FakeReader:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._idx = 0

    async def read(self, n: int) -> bytes:
        if self._idx >= len(self._chunks):
            return b""
        data = self._chunks[self._idx]
        self._idx += 1
        return data


class _FakeWriter:
    def __init__(self):
        self._written: list[bytes] = []

    def get_extra_info(self, key, default=None):
        return ("127.0.0.1", 29999) if key == "peername" else default

    def write(self, data: bytes):
        self._written.append(data)

    async def drain(self):
        pass

    def close(self):
        pass

    def messages(self) -> list[dict]:
        result = []
        for chunk in self._written:
            for line in chunk.split(b"\n"):
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result


@pytest.fixture(autouse=True)
def _clean_db():
    from sqlalchemy import delete
    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(ClaimCode))
            await session.execute(delete(NodeOwner))
            await session.execute(delete(Invite))
            await session.commit()

    asyncio.run(_setup())
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    from core import state
    state.connected_nodes.pop(_NODE_ID, None)
    yield
    state.connected_nodes.pop(_NODE_ID, None)


class TestTCPClaimACK:
    def test_valid_claim_code_sends_claim_ack(self):
        """HELLO with a valid claim code → CLAIM_ACK containing user_id."""
        from core.auth import create_claim_code

        rec = asyncio.run(create_claim_code("user-X"))
        reader = _FakeReader([_hello(claim_code=rec["code"]), b""])
        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(reader, writer))

        msgs = writer.messages()
        acks = [m for m in msgs if m.get("type") == "CLAIM_ACK"]
        assert len(acks) == 1, f"Expected CLAIM_ACK; got: {msgs}"
        assert acks[0]["user_id"] == "user-X"
        assert acks[0]["node_id"] == _NODE_ID

    def test_valid_claim_assigns_node_ownership(self):
        """After CLAIM_ACK, the node is owned by the correct user in the DB."""
        from core.auth import create_claim_code, get_node_owner

        rec = asyncio.run(create_claim_code("user-Y"))
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            _FakeWriter(),
        ))

        assert asyncio.run(get_node_owner(_NODE_ID)) == "user-Y"

    def test_valid_claim_code_is_consumed_one_shot(self):
        """The same claim code cannot be reused after a successful claim."""
        from core.auth import consume_claim_code, create_claim_code

        rec = asyncio.run(create_claim_code("user-Z"))
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            _FakeWriter(),
        ))

        assert asyncio.run(consume_claim_code(rec["code"], "other-node")) is None


class TestTCPClaimNACK:
    def test_invalid_claim_code_sends_claim_nack(self):
        """HELLO with a bogus claim code → CLAIM_NACK, not CLAIM_ACK."""
        reader = _FakeReader([_hello(claim_code="BADCODE123X"), b""])
        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(reader, writer))

        msgs = writer.messages()
        nacks = [m for m in msgs if m.get("type") == "CLAIM_NACK"]
        assert len(nacks) == 1, f"Expected CLAIM_NACK; got: {msgs}"
        assert nacks[0]["node_id"] == _NODE_ID
        assert "error" in nacks[0]

    def test_invalid_code_does_not_send_claim_ack(self):
        """An invalid code must not result in any CLAIM_ACK."""
        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code="BADCODE123X"), b""]),
            writer,
        ))
        assert not any(m.get("type") == "CLAIM_ACK" for m in writer.messages())

    def test_expired_claim_code_sends_claim_nack(self):
        """HELLO with an expired claim code → CLAIM_NACK."""
        from core.auth import create_claim_code
        from core.users import ClaimCode, async_session_maker

        rec = asyncio.run(create_claim_code("user-expired"))

        async def _expire():
            async with async_session_maker() as session:
                claim = await session.get(ClaimCode, rec["code"])
                claim.expires_at = time.time() - 60
                await session.commit()

        asyncio.run(_expire())

        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            writer,
        ))

        nacks = [m for m in writer.messages() if m.get("type") == "CLAIM_NACK"]
        assert len(nacks) == 1, f"Expected CLAIM_NACK for expired code; got: {writer.messages()}"

    def test_invalid_code_does_not_assign_ownership(self):
        """An invalid claim code must not create any node ownership record."""
        from core.auth import get_node_owner

        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code="NOTACODE9999"), b""]),
            _FakeWriter(),
        ))

        assert asyncio.run(get_node_owner(_NODE_ID)) is None


class TestTCPClaimAlreadyOwned:
    def test_already_owned_sends_claim_ack_with_note(self):
        """When a node is already owned, HELLO with claim_code → CLAIM_ACK note=already_owned."""
        from core.auth import create_claim_code, set_node_owner

        asyncio.run(set_node_owner(_NODE_ID, "existing-owner"))
        rec = asyncio.run(create_claim_code("interloper"))

        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            writer,
        ))

        msgs = writer.messages()
        acks = [m for m in msgs if m.get("type") == "CLAIM_ACK"]
        assert len(acks) == 1, f"Expected already_owned CLAIM_ACK; got: {msgs}"
        assert acks[0].get("note") == "already_owned"
        assert acks[0]["node_id"] == _NODE_ID

    def test_already_owned_ack_does_not_leak_user_id(self):
        """The already_owned CLAIM_ACK must not include user_id."""
        from core.auth import create_claim_code, set_node_owner

        asyncio.run(set_node_owner(_NODE_ID, "secret-owner"))
        rec = asyncio.run(create_claim_code("interloper"))

        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            writer,
        ))

        acks = [m for m in writer.messages() if m.get("type") == "CLAIM_ACK"]
        assert len(acks) == 1
        assert "user_id" not in acks[0], "already_owned ACK must not leak user_id"

    def test_already_owned_ownership_unchanged(self):
        """A claim attempt on an already-owned node must not change the owner."""
        from core.auth import create_claim_code, get_node_owner, set_node_owner

        asyncio.run(set_node_owner(_NODE_ID, "original-owner"))
        rec = asyncio.run(create_claim_code("interloper"))

        asyncio.run(handle_tcp_client(
            _FakeReader([_hello(claim_code=rec["code"]), b""]),
            _FakeWriter(),
        ))

        assert asyncio.run(get_node_owner(_NODE_ID)) == "original-owner"


class TestTCPNoClaimCode:
    def test_hello_without_claim_code_sends_no_claim_message(self):
        """HELLO without claim_code → no CLAIM_ACK or CLAIM_NACK."""
        writer = _FakeWriter()
        asyncio.run(handle_tcp_client(_FakeReader([_hello(), b""]), writer))

        claim_msgs = [
            m for m in writer.messages()
            if m.get("type") in ("CLAIM_ACK", "CLAIM_NACK")
        ]
        assert claim_msgs == [], f"No claim messages expected; got: {claim_msgs}"

    def test_hello_without_claim_code_does_not_crash(self):
        """A HELLO without claim_code completes without raising."""
        asyncio.run(handle_tcp_client(_FakeReader([_hello(), b""]), _FakeWriter()))
