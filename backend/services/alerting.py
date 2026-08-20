"""Lightweight webhook alerter — fires HTTP POST on critical events.

Set ALERT_WEBHOOK_URL in .env to enable. Sends a JSON payload to the
configured URL whenever a critical condition is detected.

Deduplicates alerts: same alert_type is not re-sent within ALERT_COOLDOWN_S
seconds (default 300).

Delivery is retried a bounded number of times on 5xx and transport errors,
never on 4xx; see _fire() for why the distinction matters and why the
cooldown is released only once the attempts are spent.

Settings are read from the environment on each call rather than once at
import. main.py calls load_dotenv() after its service imports, so an
import-time read sees an empty environment on any start that does not
already carry the variables (e.g. a bare `python main.py`), which leaves
alerting silently disabled.

Stays a generic webhook poster rather than a ClickUp client: ALERT_WEBHOOK_URL
carries the destination (including any workspace/channel ids it needs) and
ALERT_WEBHOOK_FORMAT selects the body shape. A second sink is a new format
branch here, not a rewrite. The `clickup_chat` format targets ClickUp's chat
message endpoint, which ClickUp documents as experimental; log_destination()
is the mitigation for that endpoint changing shape or disappearing with no
deploy of ours to blame.
"""

import logging
import os
import random
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_S = 300.0
_DEFAULT_FORMAT = "raw"
_FORMATS = (_DEFAULT_FORMAT, "clickup_chat")

# Delivery retry. Deliberately module constants rather than settings: the
# values follow from the sink's observed failure shape, not from anything an
# operator tunes per box, and every extra ALERT_* key is one more thing that
# can be set wrong on one droplet and right on another.
_MAX_DELIVERY_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.5
_MAX_BACKOFF_S = 4.0

_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    with _lock:
        _last_sent.clear()


def _webhook_url() -> str:
    return os.getenv("ALERT_WEBHOOK_URL", "").strip()


def is_enabled() -> bool:
    return bool(_webhook_url())


def _cooldown_s() -> float:
    """Read ALERT_COOLDOWN_S, falling back to the default on a malformed value.

    send_alert is called from failure-handling paths, so a stray value here
    must not raise out of it (that would turn a reportable problem into a
    second one) and must not silence the alert either.
    """
    raw = os.getenv("ALERT_COOLDOWN_S", "").strip()
    if not raw:
        return _DEFAULT_COOLDOWN_S
    try:
        return float(raw)
    except ValueError:
        logger.warning("malformed ALERT_COOLDOWN_S=%r, using default %ss", raw, _DEFAULT_COOLDOWN_S)
        return _DEFAULT_COOLDOWN_S


def _webhook_format() -> str:
    """Read ALERT_WEBHOOK_FORMAT, falling back to the default on an unrecognised value.

    Same shape as _cooldown_s(): send_alert must never raise or go silent
    over a stray setting, since it is itself called from failure paths.
    """
    raw = os.getenv("ALERT_WEBHOOK_FORMAT", "").strip()
    if not raw:
        return _DEFAULT_FORMAT
    if raw not in _FORMATS:
        logger.warning("unrecognised ALERT_WEBHOOK_FORMAT=%r, using default %r", raw, _DEFAULT_FORMAT)
        return _DEFAULT_FORMAT
    return raw


def _backoff_delay_s(attempt: int) -> float:
    """Seconds to wait after `attempt` (1-indexed) before the next one.

    Exponential from _BACKOFF_BASE_S, capped at _MAX_BACKOFF_S, then jittered
    across the upper half of that window. Jitter matters because every box
    points its alerts at the same sink: a fixed schedule would line their
    retries up into a burst at exactly the moment the sink is already
    failing. The floor is half the window rather than zero so that a retry
    cannot collapse into an immediate second POST, which is the same
    thundering-herd problem in miniature.
    """
    window = min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
    return random.uniform(window / 2, window)


def _auth_headers() -> dict[str, str]:
    """Build the Authorization header from ALERT_WEBHOOK_AUTH, sent verbatim.

    ClickUp personal tokens carry no "Bearer" prefix; adding one produces a
    401. An empty setting means no Authorization header at all, not an empty
    one.
    """
    auth = os.getenv("ALERT_WEBHOOK_AUTH", "").strip()
    return {"Authorization": auth} if auth else {}


def _render_clickup_chat(alert_type: str, message: str, meta: dict, environment: str, host: str) -> dict:
    """Render an alert as a ClickUp chat message body.

    ClickUp timestamps each message on arrival, so the channel's own message
    time stands in for the payload's timestamp field; it is not repeated in
    the rendered content. environment and host are rendered as their own
    key: value lines, ahead of meta, so which box raised the alert is
    visible in the message itself rather than depending solely on which
    channel it landed in.
    """
    lines = [f"**{alert_type}**", message, f"environment: {environment}", f"host: {host}"]
    lines.extend(f"{key}: {value}" for key, value in meta.items())
    return {"type": "message", "content": "\n".join(lines)}


def log_destination() -> None:
    """Log at INFO where alerts are going, or that they are disabled.

    Call once at startup, after load_dotenv(). This is the mitigation for
    relying on an endpoint (ClickUp's chat message API) that is documented
    as experimental: if it breaks or disappears in future, this line is the
    trail back to the dependency, rather than a silently dead alert path
    with no deploy of ours to blame. Logs the URL's scheme and host only:
    never the Authorization value, and never the full URL, which may carry
    workspace/channel ids or other identifiers in its path or query string.
    """
    webhook_url = _webhook_url()
    if not webhook_url:
        logger.info("Alerting disabled: ALERT_WEBHOOK_URL is not set")
        return
    try:
        parsed = urlsplit(webhook_url)
    except ValueError:
        # e.g. an unbalanced "[" makes urlsplit raise "Invalid IPv6 URL"
        # rather than return an unparsed result. This is best-effort startup
        # diagnostics, so a malformed value falls into the same "does not
        # parse as a URL" branch below rather than propagating: it must
        # never be able to stop the server booting.
        parsed = None
    webhook_format = _webhook_format()
    if parsed is None or not parsed.scheme or not parsed.hostname:
        logger.info("Alerting enabled: posting %s alerts (ALERT_WEBHOOK_URL does not parse as a URL)", webhook_format)
    else:
        logger.info("Alerting enabled: posting %s alerts to %s://%s", webhook_format, parsed.scheme, parsed.hostname)

    # ClickUp has no inbound webhook of its own, so the clickup_chat format
    # requires the Authorization header; without it every alert 401s and the
    # INFO line above reads as healthy while delivering nothing.
    if webhook_format == "clickup_chat" and not _auth_headers():
        logger.warning(
            "ALERT_WEBHOOK_FORMAT=clickup_chat but ALERT_WEBHOOK_AUTH is not set: "
            "ClickUp will reject every alert with 401"
        )


def send_alert(alert_type: str, message: str, meta: dict | None = None) -> None:
    """Fire a webhook alert if not in cooldown. Non-blocking (fire-and-forget)."""
    webhook_url = _webhook_url()
    if not webhook_url:
        return

    now = time.time()
    cooldown_s = _cooldown_s()
    with _lock:
        last = _last_sent.get(alert_type, 0)
        if now - last < cooldown_s:
            return
        # Stamped before the POST, not after, so that two concurrent callers
        # (the health monitor and a solver worker can both call send_alert)
        # cannot both pass the check above while one request is in flight.
        # had_previous/previous are captured so a failed delivery can put
        # the slot back exactly as it found it rather than just deleting it.
        had_previous = alert_type in _last_sent
        previous = _last_sent.get(alert_type)
        _last_sent[alert_type] = now

    meta = meta or {}

    def _release_cooldown():
        """Undo the reservation above on a failed delivery, so the next
        occurrence of the same problem is not suppressed for the rest of the
        cooldown window. Only releases the slot this call reserved: if a
        newer send has since claimed it (the stored value is no longer
        exactly `now`), that claim is left alone rather than clobbered.
        """
        with _lock:
            if _last_sent.get(alert_type) != now:
                return
            if had_previous:
                _last_sent[alert_type] = previous
            else:
                del _last_sent[alert_type]

    # Captured here, not re-read inside _fire: the thread runs later, and by
    # then the environment (or a test asserting against it) may have moved on.
    headers = _auth_headers()

    # Channel routing (a different ALERT_WEBHOOK_URL per box) is the only
    # other signal for which environment an alert came from, and a
    # copy-pasted URL would put it in the wrong channel with nothing in the
    # payload to reveal that. environment and host make that visible in the
    # alert itself. ALERT_ENVIRONMENT is a setting of its own, deliberately
    # separate from RETINA_ENV: that variable selects which backend guards
    # apply, and staging and test both hold it at `test` while the build-out
    # lasts (ClickUp 86cb1emcx), so a field sourced from it would read as
    # authoritative while leaving those two indistinguishable, and would move
    # whenever a guard decision did.
    # ALERT_ENVIRONMENT exists solely to label alerts and carries no other
    # meaning. An unset or empty value renders as "unknown" rather than
    # being omitted: a box with no environment configured is itself worth
    # seeing. gethostname() can raise (e.g. a broken resolver) or return an
    # empty string; this is best-effort identification, not the alert's
    # substance, so neither outcome may turn a reportable problem into a
    # second one.
    environment = os.getenv("ALERT_ENVIRONMENT", "").strip() or "unknown"
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"

    if _webhook_format() == "clickup_chat":
        body = _render_clickup_chat(alert_type, message, meta, environment, host)
    else:
        body = {
            "alert_type": alert_type,
            "message": message,
            "timestamp": now,
            "environment": environment,
            "host": host,
            "meta": meta,
        }

    def _fire():
        """Deliver the alert, retrying only what is worth retrying.

        ClickUp's chat API returns intermittent 500s (about one delivery in
        three, measured over 90 minutes on production, with no 429s
        anywhere), so a single POST per alert loses that share outright. The
        health monitor survives that, because its conditions are still true
        at the next cycle and the alert recurs. mender_unreachable and
        registration_held do not: both fire once, at the moment they matter,
        and a channel that looks live while dropping a third of those is
        worse than no channel at all.

        A 4xx is never retried. It is a decision the server already made
        about this token (401) or this body (400), so repeating the request
        only multiplies the same failure against a sink we depend on.

        The cooldown reservation is held for the whole sequence and released
        exactly once, once the attempts are spent. Releasing per failed
        attempt would let the next health cycle open a second delivery of
        the same alert while this one is still retrying, which is how a 500
        problem turns into a 429 problem.
        """
        for attempt in range(1, _MAX_DELIVERY_ATTEMPTS + 1):
            final_attempt = attempt == _MAX_DELIVERY_ATTEMPTS
            try:
                with httpx.Client(timeout=10.0) as client:
                    post_kwargs = {"json": body}
                    # An empty Authorization header is not the same as no header,
                    # and the existing raw sinks are called with no headers kwarg
                    # at all, so only add it when there is a header to send.
                    if headers:
                        post_kwargs["headers"] = headers
                    status = client.post(webhook_url, **post_kwargs).status_code

                if status < 400:
                    return
                if status < 500:
                    logger.error(
                        "Alert webhook rejected %s with %d, not retrying "
                        "(check ALERT_WEBHOOK_AUTH and the payload shape)",
                        alert_type,
                        status,
                    )
                    _release_cooldown()
                    return
                if final_attempt:
                    logger.error(
                        "Alert webhook returned %d for %s on all %d attempts, alert dropped",
                        status,
                        alert_type,
                        _MAX_DELIVERY_ATTEMPTS,
                    )
                    _release_cooldown()
                    return
                logger.warning(
                    "Alert webhook returned %d for %s (attempt %d/%d), retrying",
                    status,
                    alert_type,
                    attempt,
                    _MAX_DELIVERY_ATTEMPTS,
                )
            except httpx.RequestError:
                # The request never reached a handler that made a decision,
                # so it is retriable for the same reason a 5xx is.
                if final_attempt:
                    logger.error(
                        "Alert webhook unreachable for %s on all %d attempts, alert dropped",
                        alert_type,
                        _MAX_DELIVERY_ATTEMPTS,
                        exc_info=True,
                    )
                    _release_cooldown()
                    return
                logger.warning(
                    "Alert webhook unreachable for %s (attempt %d/%d), retrying",
                    alert_type,
                    attempt,
                    _MAX_DELIVERY_ATTEMPTS,
                )
            except Exception:
                # Not a transport failure: a fault in this path (a body that
                # will not serialise, say) is deterministic, so retrying it
                # just repeats the fault.
                logger.error("Alert delivery failed for %s", alert_type, exc_info=True)
                _release_cooldown()
                return

            time.sleep(_backoff_delay_s(attempt))

    threading.Thread(target=_fire, daemon=True).start()
