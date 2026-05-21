"""Periodic health monitor — owns alerting.

Evaluates the shared health checks on a fixed interval (independent of who
polls /api/health) and fires webhook alerts. Tracks which issues are currently
firing so it can:
  - send one alert per condition (keyed by issue type → per-condition cooldown),
  - emit a "resolved" alert when a previously-firing condition clears.

This decouples alerting from the request path: the server alerts on its own
schedule, and a wedged endpoint no longer means silent degradation (the
heartbeat/dead-man's-switch covers a fully-down process — see heartbeat.py).
"""

import asyncio
import logging
import os

from services.alerting import send_alert
from services.health import compute_health_issues

logger = logging.getLogger(__name__)

HEALTH_MONITOR_INTERVAL_S = float(os.getenv("HEALTH_MONITOR_INTERVAL_S", "30"))


def run_cycle(active: set[str]) -> set[str]:
    """Evaluate health once, fire alerts, and return the new active-issue set.

    `active` is the set of issue types that were firing last cycle; an alert for
    a type no longer present is sent as "resolved". Pure aside from send_alert,
    so it can be driven directly in tests.
    """
    issues = compute_health_issues()
    current = {i["type"]: i for i in issues}

    for type_, issue in current.items():
        send_alert(type_, issue["message"], {"severity": issue["severity"]})

    for type_ in active:
        if type_ not in current:
            send_alert(f"resolved:{type_}", f"Resolved: {type_}", {"severity": "info"})

    return set(current)


async def health_monitor_task() -> None:
    active: set[str] = set()
    while True:
        await asyncio.sleep(HEALTH_MONITOR_INTERVAL_S)
        try:
            active = run_cycle(active)
        except Exception:
            logger.exception("Health monitor cycle failed")
