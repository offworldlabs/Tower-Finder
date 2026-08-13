"""Unit tests for the webhook alerting helper in services/alerting.py.

The module reads ALERT_WEBHOOK_URL and ALERT_COOLDOWN_S from the environment
on each call rather than once at import (see the module docstring for why),
so these tests drive it through monkeypatch.setenv/delenv rather than
patching module attributes.
"""

import logging
import os
from unittest.mock import ANY, MagicMock, patch

import pytest

import services.alerting as _alerting
from services.alerting import is_enabled, send_alert


@pytest.fixture(autouse=True)
def _reset_last_sent():
    _alerting._last_sent.clear()
    yield
    _alerting._last_sent.clear()


@pytest.fixture(autouse=True)
def _clean_alert_env(monkeypatch):
    """Start every test with both settings unset, whatever the ambient shell
    or .env holds, so each test's setenv/delenv calls are the only source of
    truth for what the module sees."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_COOLDOWN_S", raising=False)


def _make_mock_client(status_code=200, raise_exc=None):
    """Build a context-manager-compatible httpx.Client mock."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)

    if raise_exc is not None:
        mock_client.post.side_effect = raise_exc
    else:
        mock_client.post.return_value = mock_resp

    return mock_client


def _make_sync_thread(**kwargs):
    """Thread replacement that calls target() synchronously on .start()."""
    t = MagicMock()
    t.start.side_effect = lambda: kwargs["target"]()
    return t


def _make_env_popping_sync_thread(**kwargs):
    """Thread replacement that removes ALERT_WEBHOOK_URL from the environment
    before invoking target() synchronously on .start().

    Simulates the environment changing between send_alert() capturing the
    URL and the (normally later, real) thread run. Proves _fire closes over
    a value captured at call time rather than re-reading os.environ when it
    actually runs.
    """

    def _run():
        os.environ.pop("ALERT_WEBHOOK_URL", None)
        kwargs["target"]()

    t = MagicMock()
    t.start.side_effect = _run
    return t


class TestSendAlert:
    def test_disabled_returns_without_calling_httpx(self):
        """With ALERT_WEBHOOK_URL unset, httpx.Client must never be instantiated."""
        with patch("services.alerting.httpx.Client") as mock_cls:
            send_alert("test", "msg")
        mock_cls.assert_not_called()

    def test_url_set_after_import_is_honoured(self, monkeypatch):
        """ALERT_WEBHOOK_URL set via the environment (never touching a module
        attribute) must cause send_alert to fire, posting to that URL with
        the documented payload shape. This is the case the import-time
        constant broke: the value only reaches the module if it is read at
        call time.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"alert_type": "test", "message": "msg", "timestamp": ANY, "meta": {}},
        )

    def test_url_captured_once_not_reread_when_thread_runs(self, monkeypatch):
        """The URL send_alert captures at call time must be the one the
        thread posts to, even if the environment changes before the thread
        actually runs. Guards against _fire re-reading os.environ instead of
        closing over the captured value, which would post to whatever URL
        (or none) happens to be set when the thread executes rather than the
        one in force when the alert was raised.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_env_popping_sync_thread),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"alert_type": "test", "message": "msg", "timestamp": ANY, "meta": {}},
        )

    def test_cooldown_blocks_duplicate_alert(self, monkeypatch):
        """A second call with the same alert_type within cooldown is suppressed."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("dup", "first")
            send_alert("dup", "second")

        assert mock_client.post.call_count == 1

    def test_cooldown_set_after_import_is_honoured(self, monkeypatch):
        """ALERT_COOLDOWN_S set via the environment (never touching a module
        attribute) must govern deduplication. A cooldown of 0 means the
        second call is never suppressed, which only holds if the value is
        re-read rather than fixed at import to the 300s default.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "0")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("dup", "first")
            send_alert("dup", "second")

        assert mock_client.post.call_count == 2

    def test_malformed_cooldown_does_not_raise_and_does_not_silence_alert(self, monkeypatch):
        """A malformed ALERT_COOLDOWN_S must not raise out of send_alert and
        must not silence the alert: the first call for a fresh alert_type
        still fires. See test_malformed_cooldown_falls_back_to_exactly_300
        for the pinned fallback value, and
        test_malformed_cooldown_logs_warning_with_bad_value for the warning.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "not-a-number")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("dup", "first")  # must not raise

        mock_client.post.assert_called_once()

    def test_malformed_cooldown_falls_back_to_exactly_300(self, monkeypatch):
        """Pins the fallback to the documented 300.0 default. Two send_alert
        calls microseconds apart cannot distinguish a 300s fallback from
        e.g. a 1s one (both suppress the second call), so assert the value
        _cooldown_s() actually returns rather than inferring it indirectly
        from suppression.
        """
        monkeypatch.setenv("ALERT_COOLDOWN_S", "not-a-number")
        assert _alerting._cooldown_s() == 300.0

    def test_malformed_cooldown_logs_warning_with_bad_value(self, monkeypatch, caplog):
        """Requirement 4: a malformed ALERT_COOLDOWN_S must be logged as a
        warning naming the bad value, not swallowed silently."""
        monkeypatch.setenv("ALERT_COOLDOWN_S", "not-a-number")
        with caplog.at_level(logging.WARNING):
            _alerting._cooldown_s()
        assert "not-a-number" in caplog.text

    def test_empty_cooldown_env_var_treated_as_unset(self, monkeypatch):
        """ALERT_COOLDOWN_S="" (e.g. an .env line with no value) is treated
        as unset and defaults quietly, matching services/mender.py's shape
        for MENDER_TIMEOUT_S. This differs from the old code, which raised
        ValueError at import for the same input; the change is deliberate,
        not an oversight, so it is pinned here.
        """
        monkeypatch.setenv("ALERT_COOLDOWN_S", "")
        assert _alerting._cooldown_s() == 300.0

    def test_different_alert_types_independent_cooldown(self, monkeypatch):
        """Different alert_types each have their own cooldown entry."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("type_a", "msg")
            send_alert("type_b", "msg")

        assert mock_client.post.call_count == 2

    def test_webhook_error_does_not_propagate(self, monkeypatch):
        """A network exception inside _fire() must not surface from send_alert."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client(raise_exc=Exception("network error"))

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("err", "msg")  # must not raise

        mock_client.post.assert_called_once()

    def test_webhook_4xx_response_does_not_raise(self, monkeypatch):
        """A 4xx HTTP response is logged but must not raise from send_alert."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client(status_code=400)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("4xx", "msg")  # must not raise

        mock_client.post.assert_called_once()

    def test_is_enabled_true_when_url_set(self, monkeypatch):
        """is_enabled() returns True when ALERT_WEBHOOK_URL is non-empty."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://x")
        assert is_enabled() is True

    def test_is_enabled_false_when_url_unset(self):
        """is_enabled() returns False when ALERT_WEBHOOK_URL is unset."""
        assert is_enabled() is False
