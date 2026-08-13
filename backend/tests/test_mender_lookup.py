import logging

import httpx
import pytest

from services import mender

PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"

# A real EC P-256 SPKI public key, so the fingerprint test pins a value rather than
# just a length. Derived independently of mender.py:
#   openssl ecparam -name prime256v1 -genkey -noout | openssl pkey -pubout
#   openssl pkey -pubin -outform DER | shasum -a 256
REAL_PUBKEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6KZgT+OUiuwWJGsbVh5hkfrKHKK8\n"
    "Bm+wE+K4UfIAzoA6JbXn2IH8v6gSRhuUyOX1ZwM/4UzLmNu8hHROlRK8ag==\n"
    "-----END PUBLIC KEY-----\n"
)
EXPECTED_FINGERPRINT = "deb10bddcd61fe0040321e766b15ae97bc027368c9273aae0d2594bedc476258"


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
        # No identity_data or status params on the wire: see lookup_device's docstring.
        assert "identity_data" not in request.url.params
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
                            "pubkey": REAL_PUBKEY,
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
    assert record.auth_set_fingerprint == EXPECTED_FINGERPRINT


async def test_an_unknown_device_resolves_to_none(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("retdeadbeef") is None


async def test_a_device_with_another_identity_resolves_to_none(monkeypatch):
    """The fail-closed check from lookup_device's docstring: a response identity
    that does not match the request must not resolve."""
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
    # Per the module docstring: a dead credential blocks the whole fleet.
    monkeypatch.setattr(mender, "_transport", _responds({"error": "unauthorized"}, status_code=401))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_json_200_raises_mender_unreachable(monkeypatch):
    """An error page served with a 200 is otherwise a bare JSONDecodeError; see
    lookup_device's comment for why this needs its own route to MenderUnreachable."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_json_object_body_raises_mender_unreachable(monkeypatch):
    """A JSON object rather than a device list, e.g. from an error response or a
    future envelope change; see lookup_device's comment for why this must not
    reach _record."""
    monkeypatch.setattr(mender, "_transport", _responds({"devices": []}))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_string_pubkey_resolves_without_a_fingerprint(monkeypatch):
    """A pubkey that is not a string raises AttributeError out of str.splitlines,
    not the binascii/ValueError that an undecodable string raises."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": 12345}])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_fingerprint is None


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


async def test_a_full_page_logs_a_warning_and_still_resolves(monkeypatch, caplog):
    """A device past _PER_PAGE resolves to the same None as one Mender has never
    heard of; the warning is what tells the two apart, so it must fire on a full
    page. _PER_PAGE is pinned down to keep the fixture to two devices rather than
    five hundred."""
    monkeypatch.setattr(mender, "_PER_PAGE", 2)
    devices = [_device("ret1a2b3c4d", status="pending"), _device("ret2b3c4d5e", status="pending")]
    monkeypatch.setattr(mender, "_transport", _responds(devices))
    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "pending"
    assert any("filled the page" in message for message in caplog.messages)
