"""The in-request Mender management API lookup.

One function, so registration has one external dependency and tests have one seam.
Every failure to reach Mender raises MenderUnreachable rather than resolving to
"unknown device": the two are the same 403 on the wire, but only one of them means
every enrolment in the fleet is blocked, and that difference belongs in an alert.
"""

import base64
import binascii
import hashlib
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

MENDER_SERVER = os.getenv("MENDER_SERVER", "https://hosted.mender.io").rstrip("/")
MENDER_PAT = os.getenv("MENDER_PAT", "")
MENDER_TIMEOUT_S = float(os.getenv("MENDER_TIMEOUT_S", "3.0"))

# The page size the client-side filter needs to see the whole fleet in one request.
# Phase 1 targets twelve nodes and the tenant holds fifteen devices, so this is one
# page with room to spare; a fleet past 500 needs pagination here, not a bigger number.
_PER_PAGE = 500

# Overridden by tests with an httpx.MockTransport. None means httpx's default.
_transport: httpx.AsyncBaseTransport | None = None


class MenderUnreachable(Exception):
    """Mender could not be asked. Distinct from Mender answering no."""


@dataclass(frozen=True)
class MenderDeviceRecord:
    """What one device's entry in the management API reduces to.

    The accepted set's PEM is not carried. Phase 1 has no `mender_devices` table
    to write it to and nothing reads it until the key-continuity check lands, and
    it comes back on this same call whenever that happens. The fingerprint is
    kept because it is the derived form the takeover monitors compare on.
    """

    mender_device_id: str
    node_id: str
    auth_status: str
    auth_set_id: str | None
    auth_set_fingerprint: str | None


def _fingerprint(pem: str) -> str:
    """SHA-256 over the DER SPKI, which is the base64 body of the PEM decoded."""
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    return hashlib.sha256(base64.b64decode(body)).hexdigest()


def _record(device: dict) -> MenderDeviceRecord:
    node_id = (device.get("identity_data") or {}).get("node_id", "")
    accepted = [s for s in device.get("auth_sets") or [] if s.get("status") == "accepted"]
    if len(accepted) > 1:
        # The ADR refuses rather than choosing: picking one would silently bless
        # whichever key happened to sort first on a device with two live identities.
        logger.warning("mender device %s has %d accepted auth sets", device.get("id"), len(accepted))
        return MenderDeviceRecord(device.get("id", ""), node_id, "ambiguous", None, None)
    if not accepted:
        return MenderDeviceRecord(device.get("id", ""), node_id, device.get("status", "pending"), None, None)
    auth_set = accepted[0]
    pubkey = auth_set.get("pubkey")
    fingerprint = None
    if pubkey:
        try:
            fingerprint = _fingerprint(pubkey)
        except (binascii.Error, ValueError):
            logger.warning("mender device %s has an undecodable pubkey", device.get("id"))
    return MenderDeviceRecord(
        device.get("id", ""),
        node_id,
        device.get("status", "accepted"),
        auth_set.get("id"),
        fingerprint,
    )


async def lookup_device(node_id: str) -> MenderDeviceRecord | None:
    """Resolve node_id against Mender. None means Mender does not know it.

    The filtering is client side because deviceauth has no identity filter:
    GET /devices declares status, id, page and per_page and ignores any other
    query parameter rather than rejecting it, so a filter that looks right
    returns the whole first page instead of nothing. The identity check before
    the return is what makes that fail closed, and it is the part to keep
    whatever the query becomes.

    No status filter either, deliberately. Asking only for accepted devices
    collapses "Mender has never heard of this board" and "this board is enrolled
    and waiting to be accepted" into the same None, and those want different
    operator actions. The wire cannot tell them apart (both are the shared 403),
    but the log can, and during bring-up most of the fleet is pending.
    """
    headers = {"Authorization": f"Bearer {MENDER_PAT}"}
    try:
        async with httpx.AsyncClient(base_url=MENDER_SERVER, timeout=MENDER_TIMEOUT_S, transport=_transport) as client:
            response = await client.get(
                "/api/management/v2/devauth/devices", params={"per_page": _PER_PAGE}, headers=headers
            )
    except httpx.HTTPError as exc:
        raise MenderUnreachable(str(exc)) from exc
    if response.status_code != 200:
        # A 401 lands here too: a dead credential blocks the whole fleet, which is
        # the alerting case rather than a device Mender does not know about.
        raise MenderUnreachable(f"status {response.status_code}")
    for device in response.json():
        record = _record(device)
        if record.node_id == node_id:
            return record
    return None
