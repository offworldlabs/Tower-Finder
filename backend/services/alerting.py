"""Lightweight webhook alerter — fires HTTP POST on critical events.

Set ALERT_WEBHOOK_URL in .env to enable. Sends a JSON payload to the
configured URL whenever a critical condition is detected.

Deduplicates alerts: same alert_type is not re-sent within COOLDOWN_S seconds.
"""

import logging
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
COOLDOWN_S = float(os.getenv("ALERT_COOLDOWN_S", "300"))  # 5 min default

_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def is_enabled() -> bool:
    return bool(WEBHOOK_URL)


def send_alert(alert_type: str, message: str, meta: dict | None = None) -> None:
    """Fire a webhook alert if not in cooldown. Non-blocking (fire-and-forget)."""
    if not WEBHOOK_URL:
        return

    now = time.time()
    with _lock:
        last = _last_sent.get(alert_type, 0)
        if now - last < COOLDOWN_S:
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
                resp = client.post(WEBHOOK_URL, json=payload)
                if resp.status_code >= 400:
                    logger.warning("Alert webhook returned %d for %s", resp.status_code, alert_type)
        except Exception:
            logger.warning("Alert webhook failed for %s", alert_type, exc_info=True)

    threading.Thread(target=_fire, daemon=True).start()
