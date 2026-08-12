import httpx
import pytest

from services import mender

PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


def _device(node_id: str, status: str = "accepted", auth_sets: list | None = None) -> dict:
    return {
        "id": "b7e2",
        "identity_data": {"node_id": node_id},
        "status": status,
        "auth_sets": auth_sets if auth_sets is not None else [],
    }


def _responds(payload, status_code: int = 200):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


async def test_an_accepted_device_resolves(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/management/v2/devauth/devices")
        # No identity filter on the wire: deviceauth declares only status, id, page
        # and per_page, and silently ignores anything else. Filtering happens client
        # side, so asserting an identity_data parameter here would pin the bug.
        assert "identity_data" not in request.url.params
        # No status filter either: see lookup_device's docstring. Asking only for
        # accepted devices would collapse unenrolled and pending into one None.
        assert "status" not in request.url.params
        assert request.url.params["per_page"] == "500"
        return httpx.Response(
            200,
            json=[
                _device(
                    "ret1a2b3c4d",
                    auth_sets=[
                        {
                            "id": "as-1",
                            "status": "accepted",
                            "pubkey": PUBKEY,
                            "identity_data": {"node_id": "ret1a2b3c4d"},
                        }
                    ],
                )
            ],
        )

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_id == "as-1"
    assert record.auth_set_fingerprint and len(record.auth_set_fingerprint) == 64


async def test_an_unknown_device_resolves_to_none(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("retdeadbeef") is None


async def test_a_device_with_another_identity_resolves_to_none(monkeypatch):
    """The check that makes a silently ignored filter fail closed.

    Deviceauth has no identity filter and ignores unknown query parameters rather
    than rejecting them, so a request that looks filtered returns the first page of
    the whole fleet. Without matching the returned identity against the requested
    one, a forged node_id resolves to whatever device sorts first and the
    acceptance gate inverts.
    """
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("retffffffff", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": PUBKEY}])]),
    )
    assert await mender.lookup_device("ret1a2b3c4d") is None


async def test_a_pending_device_resolves_but_is_not_accepted(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds([_device("ret1a2b3c4d", status="pending")]))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "pending"


async def test_a_timeout_raises_mender_unreachable(monkeypatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_5xx_raises_mender_unreachable(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds({"error": "down"}, status_code=503))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_401_raises_mender_unreachable(monkeypatch):
    """A dead credential blocks every enrolment, so it is the alerting case rather
    than a device Mender does not know about."""
    monkeypatch.setattr(mender, "_transport", _responds({"error": "unauthorized"}, status_code=401))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_more_than_one_accepted_set_is_refused_rather_than_resolved(monkeypatch):
    accepted = {"id": "as", "status": "accepted", "pubkey": PUBKEY, "identity_data": {"node_id": "ret1a2b3c4d"}}
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[accepted, dict(accepted, id="as2")])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "ambiguous"
    assert record.auth_set_id is None and record.auth_set_fingerprint is None
