"""One refusal shape for every reason a node request is turned away.

The status code says as little as the protocol allows, and the Retry-After must
not say more. A value derived from the reason, seconds until this node's rate
window resets for instance, would tell a caller which refusal it hit, and on the
unauthenticated registration path that difference is an oracle for which node
identities exist. Every 403 therefore carries the same body and a Retry-After
drawn from the same jittered range, whatever refused it.

The constant-latency guarantee of D32 is not implemented; see the superseded
plan for what that would add.
"""

import secrets

REFUSAL_BODY = {"error": "forbidden"}
RATE_LIMITED_BODY = {"error": "rate_limited"}

RETRY_AFTER_BASE_S = 300
RETRY_AFTER_JITTER_S = 60


def refusal_retry_after() -> int:
    """Seconds for the Retry-After header of a refusal, jittered about the base."""
    return RETRY_AFTER_BASE_S + secrets.randbelow(2 * RETRY_AFTER_JITTER_S) - RETRY_AFTER_JITTER_S
