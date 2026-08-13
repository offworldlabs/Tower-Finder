"""The limits are a security control, so every rate here is pinned by test.

Nothing sleeps: the limiters take their clock as an argument and the tests move
it by hand.
"""

import logging

import pytest

from services.node_rate_limits import ENDPOINT_LIMITS, REGISTRATION_LIMITS, RegistrationRateLimiter, TokenRateLimiter


class FakeClock:
    """A monotonic clock the test drives."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(clock: FakeClock, max_tracked: int = 64) -> TokenRateLimiter:
    return TokenRateLimiter(clock=clock, max_tracked=max_tracked)


def test_the_documented_rates_are_the_ones_configured():
    assert ENDPOINT_LIMITS == {"detection": (8, 1), "heartbeat": (30, 60), "config": (30, 60)}


def test_eight_detection_frames_a_second_are_admitted_and_the_ninth_is_not():
    clock = FakeClock()
    limiter = _limiter(clock)
    assert [limiter.check("ret1a2b3c4d", "detection") for _ in range(8)] == [None] * 8
    refusal = limiter.check("ret1a2b3c4d", "detection")
    assert refusal is not None
    assert refusal.status_code == 429
    assert refusal.body == {"error": "rate_limited"}
    assert refusal.retry_after_s >= 1


def test_the_detection_window_resets_after_a_second():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.check("ret1a2b3c4d", "detection")
    assert limiter.check("ret1a2b3c4d", "detection") is not None
    clock.advance(1.0)
    assert limiter.check("ret1a2b3c4d", "detection") is None


def test_a_refused_request_is_not_counted():
    """Hammering a closed window must not push its reset further out."""
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.check("ret1a2b3c4d", "detection")
    for _ in range(20):
        assert limiter.check("ret1a2b3c4d", "detection") is not None
    clock.advance(1.0)
    assert [limiter.check("ret1a2b3c4d", "detection") for _ in range(8)] == [None] * 8


@pytest.mark.parametrize("endpoint", ["heartbeat", "config"])
def test_thirty_a_minute_on_the_slow_endpoints(endpoint):
    clock = FakeClock()
    limiter = _limiter(clock)
    assert [limiter.check("ret1a2b3c4d", endpoint) for _ in range(30)] == [None] * 30
    assert limiter.check("ret1a2b3c4d", endpoint) is not None
    clock.advance(60.0)
    assert limiter.check("ret1a2b3c4d", endpoint) is None


def test_the_endpoints_do_not_share_a_counter():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(30):
        limiter.check("ret1a2b3c4d", "heartbeat")
    assert limiter.check("ret1a2b3c4d", "heartbeat") is not None
    assert limiter.check("ret1a2b3c4d", "config") is None
    assert limiter.check("ret1a2b3c4d", "detection") is None


def test_two_nodes_do_not_share_a_counter():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.check("ret1a2b3c4d", "detection")
    assert limiter.check("ret1a2b3c4d", "detection") is not None
    assert limiter.check("ret000000001", "detection") is None


def test_the_retry_after_counts_down_the_window_and_is_never_zero():
    # Windows are laid out against the clock rather than against first use, so
    # this one starts on a boundary to get the full sixty seconds to count down.
    clock = FakeClock(960.0)
    limiter = _limiter(clock)
    for _ in range(30):
        limiter.check("ret1a2b3c4d", "heartbeat")
    early = limiter.check("ret1a2b3c4d", "heartbeat")
    clock.advance(59.9)
    late = limiter.check("ret1a2b3c4d", "heartbeat")
    assert early.retry_after_s == 60
    assert late.retry_after_s == 1


def test_an_endpoint_with_no_configured_limit_is_a_programming_error():
    limiter = _limiter(FakeClock())
    with pytest.raises(ValueError, match="towers"):
        limiter.check("ret1a2b3c4d", "towers")


def test_a_crowded_map_reclaims_only_counters_whose_window_has_passed():
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    for n in range(5):
        limiter.check(f"ret00000000{n}", "detection")
    assert limiter.tracked_counters == 5
    clock.advance(1.0)
    limiter.check("ret000000009", "detection")
    assert limiter.tracked_counters == 1


def test_a_live_counter_survives_a_crowded_map():
    """Evicting a live counter is the same as clearing the victim's limit."""
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    for _ in range(8):
        limiter.check("ret1a2b3c4d", "detection")
    for n in range(20):
        limiter.check(f"ret00000000{n}", "detection")
    assert limiter.check("ret1a2b3c4d", "detection") is not None


def test_a_map_above_the_expected_fleet_size_warns_rather_than_evicting(caplog):
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    with caplog.at_level(logging.WARNING, logger="services.node_rate_limits"):
        for n in range(10):
            limiter.check(f"ret00000000{n}", "detection")
    assert any("evict" in record.message or "counters" in record.message for record in caplog.records)
    assert limiter.tracked_counters == 10


from services.node_refusals import RETRY_AFTER_BASE_S, RETRY_AFTER_JITTER_S


def _registration(clock: FakeClock, max_tracked: int = 64) -> RegistrationRateLimiter:
    return RegistrationRateLimiter(clock=clock, max_tracked=max_tracked)


def test_the_documented_registration_rates_are_the_ones_configured():
    assert set(REGISTRATION_LIMITS) == {(5, 3600), (20, 86400)}


def test_five_registrations_an_hour_are_admitted_and_the_sixth_is_not():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    assert [limiter.check("ret1a2b3c4d") for _ in range(5)] == [None] * 5
    refusal = limiter.check("ret1a2b3c4d")
    assert refusal is not None
    assert refusal.status_code == 403
    assert refusal.body == {"error": "forbidden"}


def test_the_registration_refusal_carries_the_shared_jittered_retry_after():
    """A countdown of this node's window would time the refusal, and timing it
    would say what the shared 403 body deliberately does not."""
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.check("ret1a2b3c4d")
    values = {limiter.check("ret1a2b3c4d").retry_after_s for _ in range(100)}
    assert all(
        RETRY_AFTER_BASE_S - RETRY_AFTER_JITTER_S <= v < RETRY_AFTER_BASE_S + RETRY_AFTER_JITTER_S for v in values
    )
    assert len(values) > 1


def test_the_hourly_window_resets():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.check("ret1a2b3c4d")
    assert limiter.check("ret1a2b3c4d") is not None
    clock.advance(3600)
    assert limiter.check("ret1a2b3c4d") is None


def test_the_daily_cap_holds_though_the_hourly_window_is_fresh():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            assert limiter.check("ret1a2b3c4d") is None
        clock.advance(3600)
    refusal = limiter.check("ret1a2b3c4d")
    assert refusal is not None
    assert refusal.status_code == 403


def test_the_daily_window_resets():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            limiter.check("ret1a2b3c4d")
        clock.advance(3600)
    assert limiter.check("ret1a2b3c4d") is not None
    clock.advance(86400)
    assert limiter.check("ret1a2b3c4d") is None


def test_a_refusal_by_the_daily_cap_does_not_spend_the_hourly_allowance():
    """Both limits have to have room before either is charged."""
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            limiter.check("ret1a2b3c4d")
        clock.advance(3600)
    for _ in range(10):
        assert limiter.check("ret1a2b3c4d") is not None
    clock.advance(86400 - 4 * 3600)
    assert [limiter.check("ret1a2b3c4d") for _ in range(5)] == [None] * 5


def test_two_node_ids_do_not_share_a_registration_counter():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.check("ret1a2b3c4d")
    assert limiter.check("ret1a2b3c4d") is not None
    assert limiter.check("ret000000001") is None


def test_a_live_registration_counter_survives_a_crowded_map():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=2)
    for _ in range(5):
        limiter.check("ret1a2b3c4d")
    for n in range(20):
        limiter.check(f"ret00000000{n}")
    assert limiter.check("ret1a2b3c4d") is not None
