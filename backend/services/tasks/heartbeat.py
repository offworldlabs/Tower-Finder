"""Dead-man's-switch heartbeat.

If HEARTBEAT_URL is set, pings it on a fixed interval. Point it at a free
external check (e.g. Healthchecks.io): the external service alerts when pings
*stop*, which is the one failure mode in-process alerting can't catch — a
crashed process, a dead host, or the disk-full deploy death-spiral. No
infrastructure to run on our side; the SaaS owns the timeout + notification.

Disabled (no-op) when HEARTBEAT_URL is unset.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "")
HEARTBEAT_INTERVAL_S = float(os.getenv("HEARTBEAT_INTERVAL_S", "60"))


async def heartbeat_task() -> None:
    if not HEARTBEAT_URL:
        logger.info("Heartbeat disabled (HEARTBEAT_URL unset)")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await client.get(HEARTBEAT_URL)
            except Exception:
                logger.warning("Heartbeat ping failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
