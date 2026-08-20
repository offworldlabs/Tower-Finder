"""Lightweight webhook alerter — fires HTTP POST on critical events.

Set ALERT_WEBHOOK_URL in .env to enable. Sends a JSON payload to the
configured URL whenever a critical condition is detected.

Deduplicates alerts: same alert_type is not re-sent within ALERT_COOLDOWN_S
seconds (default 300).

Delivery is retried a bounded number of times on 5xx, on transport errors,
and on the two transient 4xx (408 and 429, honouring Retry-After), but never
on an answer only an operator can clear: the remaining 4xx, and a 3xx, since
redirects are not followed. See _fire() for that split and for what each
outcome does to the cooldown reservation.

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

# 408 and 429 are the only 4xx that clear on their own. Every other 4xx is a
# decision the server has already made about this token or this body, and is
# terminal in the sense routes/node_responses.py gives the word for the API
# we hand our own nodes: it needs an operator, so resending cannot help.
_RETRIABLE_4XX = frozenset({408, 429})

# Retry-After is honoured up to this bound. Past it the sink is asking for a
# wait long enough that the alert would arrive stale anyway, and a daemon
# thread sitting on the payload that long is the worse of the two failures.
_MAX_RETRY_AFTER_S = 60.0

# Earliest the next send of an alert type may go after every attempt failed
# against a sink that may yet recover.
#
# Sized so that retrying never costs the sink more than it did before
# retries existed: _MAX_DELIVERY_ATTEMPTS posts per window, against one post
# per health-monitor cycle (30s by default) previously, needs a window of at
# least attempts x cycle to break even. Anything shorter means a failing
# sink is hit harder than a healthy one, which is the 500-becomes-429
# amplification the bounded retry is here to avoid. A full ALERT_COOLDOWN_S
# would go too far the other way and buy silence on precisely the alert that
# failed to arrive.
_FAILURE_REOPEN_S = 90.0

_last_sent: dict[str, float] = {}

# Earliest wall-clock time a given alert type may be sent again, set when a
# delivery failed every attempt. Held separately from _last_sent, and as an
# absolute deadline rather than a backdated send time, so that it means the
# same thing whatever ALERT_COOLDOWN_S is doing: the setting is read afresh
# on every call, so a window derived from the value in force at failure time
# would be reinterpreted against whatever value the next call happens to
# read.
_reopen_at: dict[str, float] = {}

# Alert types with a delivery thread currently running. A retry sequence can
# outlast its own cooldown (three 10s timeouts plus two Retry-After waits is
# 150s against a 300s default, and ALERT_COOLDOWN_S is set per droplet), so
# the reservation in _last_sent cannot by itself be relied on to stop the
# next health cycle opening a second, concurrent delivery of the same alert.
_in_flight: set[str] = set()

_lock = threading.Lock()


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    with _lock:
        _last_sent.clear()
        _reopen_at.clear()
        _in_flight.clear()


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


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so tests can observe the backoff schedule.

    Patching time.sleep itself would replace it for every thread in the
    process for the duration of a test, which makes an unrelated caller's
    sleep look like one of ours.
    """
    time.sleep(seconds)


def _backoff_delay_s(attempt: int) -> float:
    """Seconds to wait after `attempt` (1-indexed) before the next one.

    Exponential from _BACKOFF_BASE_S, jittered across the upper half of the
    attempt's window. Jitter matters because every box points its alerts at
    the same sink: a fixed schedule would line their retries up into a burst
    at exactly the moment the sink is already failing. The floor is half the
    window rather than zero so that a retry cannot collapse into an
    immediate second POST, which is the same thundering-herd problem in
    miniature.
    """
    window = _BACKOFF_BASE_S * (2 ** (attempt - 1))
    return random.uniform(window / 2, window)


def _retry_delay_s(attempt: int, retry_after: str | None) -> float:
    """Backoff for `attempt`, or the sink's Retry-After when it asks longer.

    Applies to every retriable answer, not just the rate limit that motivated
    it: a 503 during a maintenance window carries Retry-After as readily as a
    429 does, and in both cases the server's own answer to "when should I come
    back" beats our guess. Ignoring it is how a rate-limited client stays
    rate-limited.

    Only the delta-seconds form is read: the HTTP-date form is rare from JSON
    APIs, and this runs on the failure path, where a malformed header must not
    be able to raise. Anything unparseable, absent or non-positive falls back
    to the backoff, and the value is never allowed to shorten it.
    """
    delay = _backoff_delay_s(attempt)
    if not retry_after:
        return delay
    try:
        requested = float(str(retry_after).strip())
    except ValueError:
        return delay
    if requested <= 0:
        return delay
    return max(delay, min(requested, _MAX_RETRY_AFTER_S))


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
        # A previous delivery failed every attempt and asked not to be
        # followed too closely. Checked separately from the cooldown because
        # it outranks it: a short ALERT_COOLDOWN_S must not be able to put a
        # failing sink back under the load the retry exists to spare it.
        if now < _reopen_at.get(alert_type, 0.0):
            return
        # A delivery for this type is still running, possibly still
        # retrying. Its own reservation may already have aged out, so this
        # is the check that actually keeps deliveries from overlapping.
        if alert_type in _in_flight:
            return
        # Stamped before the POST, not after, so that two concurrent callers
        # (the health monitor and a solver worker can both call send_alert)
        # cannot both pass the check above while one request is in flight.
        _last_sent[alert_type] = now
        _reopen_at.pop(alert_type, None)
        _in_flight.add(alert_type)

    meta = meta or {}

    def _reopen_after_failed_delivery():
        """Let this alert_type be sent again _FAILURE_REOPEN_S from now,
        after a delivery that failed every attempt against a sink that may
        yet recover.

        Keeping the full reservation would buy a whole ALERT_COOLDOWN_S of
        silence on precisely the alert that failed to arrive. Clearing it
        outright, which is what this did before retries existed, puts the
        next attempt on the health monitor's cycle, so a failing sink gets
        hit harder than a healthy one. Recorded as an absolute deadline
        rather than by backdating the reservation, so that it survives
        ALERT_COOLDOWN_S changing between this failure and the next send.

        Only acts on the slot this call reserved: if a newer send has since
        claimed it (the stored value is no longer exactly `now`), that claim
        is left alone rather than clobbered.
        """
        with _lock:
            if _last_sent.get(alert_type) != now:
                return
            del _last_sent[alert_type]
            _reopen_at[alert_type] = now + _FAILURE_REOPEN_S

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

        What may be retried follows the same three-way split that
        routes/node_responses.py hands our own nodes as `x-retry`. A 5xx or
        a transport error is `backoff`: the request never reached a handler
        that made a decision. A 429 or 408 is `retry-after`: transient, and
        the response carries the sink's own answer to when to come back. Any
        other 4xx is `never`: the server has decided about this token (401)
        or this body (400), so resending multiplies one failure into three
        against a sink we already depend on.

        The cooldown reservation is held across the whole sequence rather
        than dropped per failed attempt, so the next health cycle cannot
        open a second delivery of the same alert while this one is still
        retrying. What happens to it afterwards depends on why the delivery
        failed: a terminal 4xx keeps it, because nothing will change until
        an operator acts and re-reporting every cycle only floods the
        channel this work exists to make trustworthy; an exhausted retriable
        failure shortens it, because the sink may well be back before the
        full window is out.
        """
        post_kwargs = {"json": body}
        # An empty Authorization header is not the same as no header, and the
        # existing raw sinks are called with no headers kwarg at all, so only
        # add it when there is a header to send.
        if headers:
            post_kwargs["headers"] = headers

        try:
            # One client for the whole sequence: a retry against a sink that
            # is already struggling should reuse the connection rather than
            # pay for a fresh pool and TLS handshake each time.
            with httpx.Client(timeout=10.0) as client:
                for attempt in range(1, _MAX_DELIVERY_ATTEMPTS + 1):
                    final_attempt = attempt == _MAX_DELIVERY_ATTEMPTS
                    exc = None
                    try:
                        resp = client.post(webhook_url, **post_kwargs)
                        status = resp.status_code
                        if 200 <= status < 300:
                            return
                        reason = f"returned {status}"
                        if status >= 500 or status in _RETRIABLE_4XX:
                            delay = _retry_delay_s(attempt, resp.headers.get("Retry-After"))
                        else:
                            # Terminal: keep the reservation, so a stale token
                            # is reported once a cooldown rather than once a
                            # health cycle.
                            #
                            # A 3xx lands here too. Redirects are deliberately
                            # not followed (301/302/303 would turn the POST
                            # into a GET and deliver nothing anyway), so a
                            # redirecting URL means the alert went nowhere,
                            # and treating < 400 as success would report that
                            # as a healthy channel.
                            logger.error(
                                "Alert webhook %s for %s, not retrying (%s)",
                                reason,
                                alert_type,
                                "redirects are not followed, so nothing was delivered: "
                                "point ALERT_WEBHOOK_URL at the final destination"
                                if status < 400
                                else "check ALERT_WEBHOOK_AUTH and the payload shape",
                            )
                            return
                    except httpx.RequestError as e:
                        exc = e
                        reason = "was unreachable"
                        delay = _backoff_delay_s(attempt)

                    if final_attempt:
                        logger.error(
                            "Alert webhook %s for %s on all %d attempts, alert dropped",
                            reason,
                            alert_type,
                            _MAX_DELIVERY_ATTEMPTS,
                            exc_info=exc,
                        )
                        _reopen_after_failed_delivery()
                        return
                    logger.warning(
                        "Alert webhook %s for %s (attempt %d/%d), retrying",
                        reason,
                        alert_type,
                        attempt,
                        _MAX_DELIVERY_ATTEMPTS,
                    )
                    _sleep(delay)
        except Exception:
            # Not a transport failure: a fault anywhere else in this path (a
            # body that will not serialise, say) is deterministic, so
            # retrying it just repeats the fault. The sink itself is not
            # implicated, so the slot reopens as for any other lost alert.
            logger.error("Alert delivery failed for %s", alert_type, exc_info=True)
            _reopen_after_failed_delivery()
        finally:
            with _lock:
                _in_flight.discard(alert_type)

    try:
        threading.Thread(target=_fire, daemon=True).start()
    except Exception:
        # Thread creation itself failed, so nothing will ever clear the
        # in-flight marker: leaving it set would silence this alert type for
        # the rest of the process's life.
        with _lock:
            _in_flight.discard(alert_type)
        logger.error("Could not start alert delivery thread for %s", alert_type, exc_info=True)
        _reopen_after_failed_delivery()
