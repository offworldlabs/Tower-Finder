"""In-process fixed-window rate limits for the node API.

Two limiters, because the fleet reaches the server two different ways. The
authenticated paths are limited per (node_id, endpoint) and refuse with a 429:
the caller already holds a token, so telling it that it is going too fast leaks
nothing it does not know. Registration has no token, so it is limited on the
node_id in the request body and refuses with the shared 403; see task 3 below.

Detections are held to 8 a second, sized against the 2 Hz contract ceiling
rather than the roughly 1.1 Hz measured cadence of D45. A node cannot exceed one
frame per CPI, and sizing this against today's measurement would start refusing
frames the day blah2 gets faster. Heartbeat and config get 30 a minute each,
both being needed precisely when a node is in trouble.

Fixed windows rather than sliding: a node can send double its rate across a
window boundary, which is harmless at these sizes and a great deal easier to
reason about. Only admitted requests are counted, so a caller hammering a closed
window cannot push its reset further out.

The refusal is returned as data rather than a response. The route layer owns
FastAPI; this module owns nothing but a clock and a dict.
"""

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from services.node_refusals import RATE_LIMITED_BODY, REFUSAL_BODY, refusal_retry_after

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

# The bound raises the alarm rather than evicting, so it sits well above the
# fleet: fifty nodes across three limited endpoints need 150 counters, and this
# leaves room for several times that before anything is said.
MAX_TRACKED_KEYS = 512
OVERFLOW_LOG_INTERVAL_S = 60.0

# endpoint -> (requests admitted, window in seconds)
ENDPOINT_LIMITS: dict[str, tuple[int, int]] = {
    "detection": (8, 1),
    "heartbeat": (30, 60),
    "config": (30, 60),
}


@dataclass(frozen=True)
class Refusal:
    """What the route should send back. Rendering it is the route's business."""

    status_code: int
    body: dict[str, str]
    retry_after_s: int


class _FixedWindowCounters:
    """Counts admitted requests per key per window, and never evicts a live one.

    Keys map to (window_end, count). Storing the end of the window rather than
    its index means an entry says for itself whether it is still live, which is
    what makes reclaiming the dead ones safe.

    The lock is cheap and the counters are the only bound on the registration
    path, so it is not worth reasoning about which caller might one day run off
    the event loop.
    """

    def __init__(self, clock: Clock, max_tracked: int) -> None:
        self._clock = clock
        self._max_tracked = max_tracked
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, object], tuple[float, int]] = {}
        self._next_overflow_log = 0.0

    @property
    def tracked_counters(self) -> int:
        with self._lock:
            return len(self._counters)

    def admit(self, specs: Sequence[tuple[tuple[str, object], int, int]]) -> float | None:
        """Admit one request against every spec, or report the wait in seconds.

        Every spec must have room, and only then is every spec incremented: a
        request refused by the daily limit must not consume the hourly one.
        """
        now = self._clock()
        with self._lock:
            self._reclaim_if_crowded(now)
            for key, limit, _window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end > now and count >= limit:
                    return window_end - now
            for key, _limit, window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end <= now:
                    window_end, count = (math.floor(now / window_s) + 1) * window_s, 0
                self._counters[key] = (window_end, count + 1)
            return None

    def _reclaim_if_crowded(self, now: float) -> None:
        """Drop counters whose window has passed, and warn about what is left.

        A live counter is never evicted. Flushing a victim's counter is the same
        as clearing its limit, so an attacker able to force an eviction would
        have removed the control; a map larger than the fleet is a thing to
        raise the alarm about, not to trim.
        """
        if len(self._counters) <= self._max_tracked:
            return
        for key in [key for key, (window_end, _) in self._counters.items() if window_end <= now]:
            del self._counters[key]
        if len(self._counters) > self._max_tracked and now >= self._next_overflow_log:
            self._next_overflow_log = now + OVERFLOW_LOG_INTERVAL_S
            logger.warning(
                "node rate limit map holds %d live counters, above the %d expected of the fleet; "
                "nothing has been evicted",
                len(self._counters),
                self._max_tracked,
            )


class TokenRateLimiter:
    """Per (node_id, endpoint), for the three paths that carry a bearer token."""

    def __init__(self, clock: Clock = time.monotonic, max_tracked: int = MAX_TRACKED_KEYS) -> None:
        self._counters = _FixedWindowCounters(clock, max_tracked)

    @property
    def tracked_counters(self) -> int:
        return self._counters.tracked_counters

    def check(self, node_id: str, endpoint: str) -> Refusal | None:
        """None if the request is admitted, otherwise the refusal to send."""
        if endpoint not in ENDPOINT_LIMITS:
            raise ValueError(f"no rate limit configured for endpoint {endpoint!r}")
        limit, window_s = ENDPOINT_LIMITS[endpoint]
        wait_s = self._counters.admit([((node_id, endpoint), limit, window_s)])
        if wait_s is None:
            return None
        return Refusal(429, RATE_LIMITED_BODY, max(1, math.ceil(wait_s)))


TOKEN_LIMITER = TokenRateLimiter()


# (requests admitted, window in seconds), from the ADR's table. The escalating
# cooldown it also specified is out of scope for this phase.
REGISTRATION_LIMITS: tuple[tuple[int, int], ...] = ((5, 3600), (20, 86400))


class RegistrationRateLimiter:
    """Per node_id, for the one endpoint with no token to key on.

    This is the only control bounding the identity-takeover path that
    re-registration accepts: a node Mender still accepts may register again and
    the incumbent token is revoked, so the rate at which that can be attempted is
    the whole of the defence.
    """

    def __init__(self, clock: Clock = time.monotonic, max_tracked: int = MAX_TRACKED_KEYS) -> None:
        self._counters = _FixedWindowCounters(clock, max_tracked)

    @property
    def tracked_counters(self) -> int:
        return self._counters.tracked_counters

    def check(self, node_id: str) -> Refusal | None:
        """None if the registration may proceed, otherwise the shared refusal.

        The refusal is the same 403 an unknown device gets, with the same
        jittered Retry-After. A 429, or a Retry-After counting down this node's
        window, would confirm to an unauthenticated caller that the identity it
        named is one the server is tracking.
        """
        specs = [((node_id, window_s), limit, window_s) for limit, window_s in REGISTRATION_LIMITS]
        if self._counters.admit(specs) is None:
            return None
        return Refusal(403, REFUSAL_BODY, refusal_retry_after())


REGISTRATION_LIMITER = RegistrationRateLimiter()
