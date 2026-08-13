"""Lightweight webhook alerter — fires HTTP POST on critical events.

Set ALERT_WEBHOOK_URL in .env to enable. Sends a JSON payload to the
configured URL whenever a critical condition is detected.

Deduplicates alerts: same alert_type is not re-sent within ALERT_COOLDOWN_S
seconds (default 300).

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
import threading
import time
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_S = 300.0
_DEFAULT_FORMAT = "raw"
_FORMATS = (_DEFAULT_FORMAT, "clickup_chat")

_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    with _lock:
        _last_sent.clear()


def _webhook_url() -> str:
    return os.getenv("ALERT_WEBHOOK_URL", "")


def is_enabled() -> bool:
    return bool(_webhook_url())


def _cooldown_s() -> float:
    """Read ALERT_COOLDOWN_S, falling back to the default on a malformed value.

    send_alert is called from failure-handling paths, so a stray value here
    must not raise out of it (that would turn a reportable problem into a
    second one) and must not silence the alert either.
    """
    raw = os.getenv("ALERT_COOLDOWN_S", "")
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
    raw = os.getenv("ALERT_WEBHOOK_FORMAT", "")
    if not raw:
        return _DEFAULT_FORMAT
    if raw not in _FORMATS:
        logger.warning("unrecognised ALERT_WEBHOOK_FORMAT=%r, using default %r", raw, _DEFAULT_FORMAT)
        return _DEFAULT_FORMAT
    return raw


def _auth_headers() -> dict[str, str]:
    """Build the Authorization header from ALERT_WEBHOOK_AUTH, sent verbatim.

    ClickUp personal tokens carry no "Bearer" prefix; adding one produces a
    401. An empty setting means no Authorization header at all, not an empty
    one.
    """
    auth = os.getenv("ALERT_WEBHOOK_AUTH", "")
    return {"Authorization": auth} if auth else {}


def _render_clickup_chat(alert_type: str, message: str, meta: dict) -> dict:
    """Render an alert as a ClickUp chat message body.

    ClickUp timestamps each message on arrival, so the channel's own message
    time stands in for the payload's timestamp field; it is not repeated in
    the rendered content.
    """
    lines = [f"**{alert_type}**", message]
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
    if parsed is None or not parsed.scheme or not parsed.hostname:
        logger.info(
            "Alerting enabled: posting %s alerts (ALERT_WEBHOOK_URL does not parse as a URL)", _webhook_format()
        )
        return
    logger.info("Alerting enabled: posting %s alerts to %s://%s", _webhook_format(), parsed.scheme, parsed.hostname)


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
        _last_sent[alert_type] = now

    meta = meta or {}

    # Captured here, not re-read inside _fire: the thread runs later, and by
    # then the environment (or a test asserting against it) may have moved on.
    headers = _auth_headers()
    if _webhook_format() == "clickup_chat":
        body = _render_clickup_chat(alert_type, message, meta)
    else:
        body = {
            "alert_type": alert_type,
            "message": message,
            "timestamp": now,
            "meta": meta,
        }

    def _fire():
        try:
            with httpx.Client(timeout=10.0) as client:
                post_kwargs = {"json": body}
                # An empty Authorization header is not the same as no header,
                # and the existing raw sinks are called with no headers kwarg
                # at all, so only add it when there is a header to send.
                if headers:
                    post_kwargs["headers"] = headers
                resp = client.post(webhook_url, **post_kwargs)
                if resp.status_code >= 400:
                    logger.warning("Alert webhook returned %d for %s", resp.status_code, alert_type)
        except Exception:
            logger.warning("Alert webhook failed for %s", alert_type, exc_info=True)

    threading.Thread(target=_fire, daemon=True).start()
