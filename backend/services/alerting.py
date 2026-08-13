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
"""

import logging
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_S = 300.0

_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    with _lock:
        _last_sent.clear()


def is_enabled() -> bool:
    return bool(os.getenv("ALERT_WEBHOOK_URL", ""))


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


def send_alert(alert_type: str, message: str, meta: dict | None = None) -> None:
    """Fire a webhook alert if not in cooldown. Non-blocking (fire-and-forget)."""
    webhook_url = os.getenv("ALERT_WEBHOOK_URL", "")
    if not webhook_url:
        return

    now = time.time()
    with _lock:
        last = _last_sent.get(alert_type, 0)
        if now - last < _cooldown_s():
            return
        _last_sent[alert_type] = now

    payload = {
        "alert_type": alert_type,
        "message": message,
        "timestamp": now,
        "meta": meta or {},
    }

    def _fire():
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(webhook_url, json=payload)
                if resp.status_code >= 400:
                    logger.warning("Alert webhook returned %d for %s", resp.status_code, alert_type)
        except Exception:
            logger.warning("Alert webhook failed for %s", alert_type, exc_info=True)

    threading.Thread(target=_fire, daemon=True).start()
