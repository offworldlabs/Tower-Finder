"""The in-request Mender management API lookup.

One function, so registration has one external dependency and tests have one seam.
Every failure to reach Mender raises MenderUnreachable rather than resolving to
"unknown device": the two are the same 403 on the wire, but only one of them means
every enrolment in the fleet is blocked, and that difference belongs in an alert.
"""

import base64
import hashlib
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# The page size the client-side filter needs to see the whole fleet in one request.
# Phase 1 targets twelve nodes, comfortably inside one page; a fleet past 500 needs
# pagination here, not a bigger number.
_PER_PAGE = 500

# Overridden by tests with an httpx.MockTransport. None means httpx's default.
_transport: httpx.AsyncBaseTransport | None = None


class MenderUnreachable(Exception):
    """Mender could not be asked. Distinct from Mender answering no."""


@dataclass(frozen=True)
class MenderDeviceRecord:
    """What one device's entry in the management API reduces to.

    The accepted set's raw pubkey is carried alongside its fingerprint. The Mender
    mirror (D26) needs the pubkey itself to populate `mender_devices.auth_set_pubkey`,
    which is free to write as the sweep runs and expensive to backfill across the
    fleet afterwards, and registration signing (D31) cannot verify anything without
    it either. The fingerprint cannot stand in for either: it is a SHA-256 over the
    DER SPKI, so it confirms a key you already hold but cannot verify a signature.
    """

    mender_device_id: str
    node_id: str
    auth_status: str
    auth_set_id: str | None
    auth_set_pubkey: str | None
    auth_set_fingerprint: str | None


def _fingerprint(pem: str) -> str:
    """SHA-256 over the DER SPKI, which is the base64 body of the PEM decoded."""
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    # validate=True: without it, b64decode silently drops characters outside the
    # base64 alphabet instead of raising, so a non-PEM key (an OpenSSH line, say)
    # would fingerprint the wrong bytes rather than hit the except clause below.
    return hashlib.sha256(base64.b64decode(body, validate=True)).hexdigest()


def _str_or_none(value: object) -> str | None:
    """Coerce a value onto a field typed str | None: anything else is a malformed record."""
    return value if isinstance(value, str) else None


def _identity_node_id(device: dict) -> str:
    """The device's identity_data.node_id, or "" if there is no identity_data.

    deviceauth's older API carried identity data as a JSON-encoded string; that
    or a list here would raise AttributeError out of .get, the same shape
    failure closed for the device list and for auth_sets. A node_id that is
    present but not a string is the same shape failure: it can never equal the
    requested string, so left unchecked it would pass for an ordinary miss
    rather than the malformed record it is.
    """
    identity_data = device.get("identity_data")
    if identity_data is not None and not isinstance(identity_data, dict):
        raise MenderUnreachable(f"device {device.get('id')} has malformed identity_data")
    node_id = (identity_data or {}).get("node_id", "")
    if not isinstance(node_id, str):
        raise MenderUnreachable(f"device {device.get('id')} has a non-string node_id")
    return node_id


def _record(device: dict) -> MenderDeviceRecord:
    node_id = _identity_node_id(device)
    auth_sets = device.get("auth_sets") or []
    if not isinstance(auth_sets, list) or not all(isinstance(s, dict) for s in auth_sets):
        # auth_sets as a dict would iterate its string keys into s.get and raise
        # AttributeError; a list containing a non-dict element fails the same way.
        # Both are Mender answering something unintelligible, not "no accepted set".
        raise MenderUnreachable(f"device {device.get('id')} has a malformed auth_sets")
    accepted = [s for s in auth_sets if s.get("status") == "accepted"]
    if len(accepted) > 1:
        # The phase 1 ADR (claude-shared/docs/decisions/2026-08-03-node-server-phase-1.md)
        # refuses rather than choosing: picking one would silently bless whichever key
        # happened to sort first on a device with two live identities.
        logger.warning("mender device %s has %d accepted auth sets", device.get("id"), len(accepted))
        return MenderDeviceRecord(device.get("id", ""), node_id, "ambiguous", None, None, None)
    if not accepted:
        status = device.get("status", "pending")
        if status == "accepted":
            # The device's own status says accepted while nothing in auth_sets
            # agrees: the same contradiction as more than one accepted set, so
            # it gets the same refusal rather than trusting the aggregate.
            logger.warning("mender device %s is accepted with no accepted auth set", device.get("id"))
            return MenderDeviceRecord(device.get("id", ""), node_id, "ambiguous", None, None, None)
        return MenderDeviceRecord(device.get("id", ""), node_id, status, None, None, None)
    auth_set = accepted[0]
    status = device.get("status", "accepted")
    if status != "accepted":
        # The opposite contradiction to the one above (an accepted auth set
        # while the top-level status disagrees) is not symmetric with it in
        # consequence, so it is not refused the same way: trusting the
        # aggregate above would admit a node with no key identified, whereas
        # here the aggregate is the conservative reading, and auth_status
        # below carries `status` rather than "accepted", so a caller gating
        # registration on auth_status already fails closed on it exactly as
        # refusing outright would. mender-auto-accept sweeps every 30 seconds,
        # so a device caught here is ordinarily mid bring-up rather than
        # faulty; the warning exists only to make the disagreement visible.
        logger.warning("mender device %s has an accepted auth set but device status %s", device.get("id"), status)
    pubkey = auth_set.get("pubkey")
    # auth_set_id and auth_set_pubkey are both str | None, so a value that is
    # not a string (a malformed record) must not reach either field as-is. A
    # pubkey that fails to decode below is kept regardless: it is exactly the
    # one worth carrying for diagnosis.
    auth_set_id = _str_or_none(auth_set.get("id"))
    auth_set_pubkey = _str_or_none(pubkey)
    fingerprint = None
    if pubkey:
        try:
            fingerprint = _fingerprint(pubkey)
        except (ValueError, AttributeError):
            # ValueError covers undecodable base64 (binascii.Error is a subclass);
            # AttributeError covers a pubkey that is not a string at all.
            logger.warning("mender device %s has an undecodable pubkey", device.get("id"))
    return MenderDeviceRecord(
        device.get("id", ""),
        node_id,
        status,
        auth_set_id,
        auth_set_pubkey,
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
    # Read per call, not at import: backend/main.py calls load_dotenv() after its
    # service imports, so a module-level read here would see an empty MENDER_PAT
    # on a non-compose run and misreport a dead credential as MenderUnreachable.
    #
    # `or` rather than getenv's own default, on this line and MENDER_TIMEOUT_S
    # below: a variable that is set but empty (a bare `MENDER_SERVER=` line in
    # .env, passed through by compose's env_file) is "" from getenv, not None,
    # so getenv's own default never fires.
    mender_server = (os.getenv("MENDER_SERVER") or "https://hosted.mender.io").rstrip("/")
    mender_pat = os.getenv("MENDER_PAT", "")
    try:
        mender_timeout_s = float(os.getenv("MENDER_TIMEOUT_S") or "3.0")
    except ValueError as exc:
        # A stray value here should not turn into an uncaught 500.
        raise MenderUnreachable(f"malformed MENDER_TIMEOUT_S: {exc}") from exc
    headers = {"Authorization": f"Bearer {mender_pat}"}
    try:
        async with httpx.AsyncClient(base_url=mender_server, timeout=mender_timeout_s, transport=_transport) as client:
            response = await client.get(
                "/api/management/v2/devauth/devices", params={"per_page": _PER_PAGE}, headers=headers
            )
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        # A malformed MENDER_SERVER does not necessarily raise httpx.HTTPError:
        # httpx.InvalidURL (a non-ASCII host that fails IDNA encoding, a
        # malformed IPv6 literal) inherits Exception rather than HTTPError in
        # httpx 0.27, and a value with no recognisable scheme reaches urllib
        # as a bare ValueError. Both are as much a failure to reach Mender as
        # anything HTTPError already covers, and must not escape as an
        # uncaught exception.
        raise MenderUnreachable(str(exc)) from exc
    if response.status_code != 200:
        # A 401 lands here too, per the module docstring: it blocks the whole fleet.
        raise MenderUnreachable(f"status {response.status_code}")
    # A 200 does not guarantee a device list: neither of the next two checks is
    # an httpx.HTTPError, so each needs its own explicit route to MenderUnreachable
    # rather than surfacing as an uncaught exception downstream.
    try:
        payload = response.json()
    except ValueError as exc:
        # A 200 with a body that is not JSON at all, e.g. an error page from
        # something in front of Mender.
        raise MenderUnreachable(f"non-JSON response body: {exc}") from exc
    if not isinstance(payload, list):
        # A JSON object body, from an error response or a future envelope change,
        # would otherwise iterate its string keys into _record and fail there
        # instead of here.
        raise MenderUnreachable(f"expected a list of devices, got {type(payload).__name__}")
    if len(payload) >= _PER_PAGE:
        # A full page means the fleet may have grown past what one request returns,
        # and a real device sitting beyond it resolves to the same None as one
        # Mender has never heard of. This is what would tell the two apart.
        logger.warning("mender device list filled the page (%d devices); the fleet may exceed _PER_PAGE", len(payload))
    # A malformed element (not a dict, or an unparseable identity_data) is skipped rather than raised on
    # the spot: the loop runs over every device in the page, so raising here would let one unrelated bad
    # record block every node's registration. The skip is forgiven only if the scan still finds its
    # match; otherwise the skipped record could have been the one asked for (see the module docstring).
    skipped = False
    for device in payload:
        if not isinstance(device, dict):
            # E.g. ["unauthorized"], or a future list of bare device ids.
            logger.warning("mender device list contains a non-object element of type %s", type(device).__name__)
            skipped = True
            continue
        try:
            identity = _identity_node_id(device)
        except MenderUnreachable:
            logger.warning("mender device %s has malformed identity_data", device.get("id"))
            skipped = True
            continue
        if identity != node_id:
            continue
        record = _record(device)
        # Deliberately redundant now that node_id is matched above: this is the
        # fail-closed guard the docstring describes, kept for when the query
        # changes and might rely on a filter deviceauth silently ignores.
        if record.node_id == node_id:
            return record
    if skipped:
        raise MenderUnreachable("a malformed device in the list could not be checked against the requested node_id")
    return None
