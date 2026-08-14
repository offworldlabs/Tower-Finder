"""The refusal shape is a security control, so it is pinned by test."""

from services.node_refusals import (
    RATE_LIMITED_BODY,
    REFUSAL_BODY,
    RETRY_AFTER_BASE_S,
    RETRY_AFTER_JITTER_S,
    refusal_retry_after,
)


def test_the_two_bodies_are_the_documented_ones():
    assert REFUSAL_BODY == {"error": "forbidden"}
    assert RATE_LIMITED_BODY == {"error": "rate_limited"}


def test_the_retry_after_stays_inside_the_jittered_range():
    values = [refusal_retry_after() for _ in range(500)]
    assert all(
        RETRY_AFTER_BASE_S - RETRY_AFTER_JITTER_S <= v < RETRY_AFTER_BASE_S + RETRY_AFTER_JITTER_S for v in values
    )


def test_the_retry_after_is_jittered_rather_than_constant():
    """A constant value would make a refusal timeable and so distinguishable."""
    assert len({refusal_retry_after() for _ in range(100)}) > 1


def test_the_retry_after_is_a_whole_number_of_seconds():
    assert all(isinstance(refusal_retry_after(), int) for _ in range(20))
