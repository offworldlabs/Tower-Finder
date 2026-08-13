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


async def test_a_list_of_non_dict_elements_raises_mender_unreachable(monkeypatch):
    """A 200 carrying ["unauthorized"], or a future list of bare device ids, is
    skipped rather than matched; see lookup_device's comment for why a scan
    that ends with a skip and no match still raises rather than returning None."""
    monkeypatch.setattr(mender, "_transport", _responds(["unauthorized"]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_list_auth_sets_raises_mender_unreachable(monkeypatch):
    """auth_sets as a dict would iterate its string keys into s.get and raise
    AttributeError instead of the shared MenderUnreachable."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets={"status": "accepted"})]),
    )
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_an_auth_set_list_with_a_non_dict_element_raises_mender_unreachable(monkeypatch):
    """See _record's comment: a list containing a non-dict element fails the
    same way as auth_sets being a dict outright."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted"}, "garbage"])]),
    )
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_identity_data_as_a_string_raises_mender_unreachable(monkeypatch):
    """See _identity_node_id's comment for why a JSON-encoded identity_data
    string, rather than an object, must not reach .get. This is the only device
    in the response, so the skip in lookup_device's loop leaves no match and
    the scan still raises; see the malformed-device tests below for the case
    where a good match is found despite a skip elsewhere in the page."""
    device = _device("ret1a2b3c4d")
    device["identity_data"] = '{"node_id": "ret1a2b3c4d"}'
    monkeypatch.setattr(mender, "_transport", _responds([device]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_malformed_device_ahead_of_the_requested_one_still_resolves_it(monkeypatch):
    """See lookup_device's comment for why a malformed element is skipped
    rather than raised on the spot: one bad record earlier in the page must
    not stop the scan from reaching the device actually asked for."""
    bad = _device("ret00000000")
    bad["identity_data"] = '{"node_id": "ret00000000"}'
    good = _device(
        "ret1a2b3c4d",
        auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": PUBKEY, "identity_data": {"node_id": "ret1a2b3c4d"}}],
    )
    monkeypatch.setattr(mender, "_transport", _responds([bad, "garbage", good]))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_id == "as-1"


async def test_a_malformed_device_with_no_match_raises_mender_unreachable(monkeypatch):
    """A skip with no match is indistinguishable from the skipped record having
    been the one asked for, so it must not resolve to the same None as a clean
    miss; see lookup_device's comment."""
    bad = _device("ret00000000")
    bad["identity_data"] = '{"node_id": "ret00000000"}'
    other = _device("retffffffff")
    monkeypatch.setattr(mender, "_transport", _responds([other, bad]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_pem_pubkey_resolves_without_a_fingerprint(monkeypatch):
    """An OpenSSH-style key has no '-----' lines, so the whole string becomes the
    decode body, and its stripped length is still a multiple of 4; see
    _fingerprint's comment for why validate=True is what rejects it rather than
    the length check alone."""
    ssh_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKrPxK2u4pJd3vN7QeR1sT9wXyZ0aBcDeFgHiJkLmNoP al@host"
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": ssh_key}])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_fingerprint is None


async def test_mender_pat_set_after_import_is_picked_up(monkeypatch):
    """MENDER_PAT is read inside lookup_device, not at import, so a value set on
    the environment after the module has loaded (e.g. by load_dotenv() running
    later in backend/main.py) still reaches the request."""
    seen_auth = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_auth["header"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    monkeypatch.setenv("MENDER_PAT", "late-token")
    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    await mender.lookup_device("ret1a2b3c4d")
    assert seen_auth["header"] == "Bearer late-token"


async def test_a_malformed_timeout_raises_mender_unreachable(monkeypatch):
    """See lookup_device's comment for why MENDER_TIMEOUT_S is read per call
    rather than at import; the mock transport is set so a build that regresses
    to an import-time read (env var not yet malformed) still hits an assertion,
    rather than falling through to a real request against MENDER_SERVER."""
    monkeypatch.setenv("MENDER_TIMEOUT_S", "not-a-number")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_an_empty_timeout_falls_back_to_the_default(monkeypatch):
    """A bare `MENDER_TIMEOUT_S=` line in .env, passed through by compose's
    env_file, is "" from os.getenv, not unset; see lookup_device's comment for
    why that must fall back to the default rather than reach float()."""
    monkeypatch.setenv("MENDER_TIMEOUT_S", "")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("ret1a2b3c4d") is None


async def test_an_empty_server_falls_back_to_the_default(monkeypatch):
    """Same set-but-empty case as MENDER_TIMEOUT_S above, see lookup_device's
    comment: a bare `MENDER_SERVER=` line in .env leaves base_url empty rather
    than reaching the default, and httpx then has no host to send the request
    to at all."""
    monkeypatch.setenv("MENDER_SERVER", "")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("ret1a2b3c4d") is None


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


async def test_accepted_status_with_no_accepted_auth_set_is_ambiguous(monkeypatch, caplog):
    """See _record's comment: a device claiming accepted at the top level while
    no auth set agrees is the same contradiction as more than one accepted set,
    so it resolves the same way rather than trusting the aggregate status."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", status="accepted", auth_sets=[{"id": "as-1", "status": "pending"}])]),
    )
    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "ambiguous"
    assert record.auth_set_id is None and record.auth_set_fingerprint is None
    assert any("no accepted auth set" in message for message in caplog.messages)


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
