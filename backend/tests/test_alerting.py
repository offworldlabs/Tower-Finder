"""Unit tests for the webhook alerting helper in services/alerting.py.

The module uses module-level globals (WEBHOOK_URL, _last_sent). These are
monkey-patched per test to keep them isolated.
"""

from unittest.mock import MagicMock, patch

import pytest

import services.alerting as _alerting
from services.alerting import is_enabled, send_alert


@pytest.fixture(autouse=True)
def _reset_last_sent():
    _alerting._last_sent.clear()
    yield
    _alerting._last_sent.clear()


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


class TestSendAlert:
    def test_disabled_returns_without_calling_httpx(self, monkeypatch):
        """With WEBHOOK_URL='', httpx.Client must never be instantiated."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "")
        with patch("services.alerting.httpx.Client") as mock_cls:
            send_alert("test", "msg")
        mock_cls.assert_not_called()

    def test_enabled_fires_http_post(self, monkeypatch):
        """With a valid URL, the _fire thread should POST to the webhook."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with patch("services.alerting.httpx.Client", return_value=mock_client), \
             patch("services.alerting.threading.Thread", side_effect=_make_sync_thread):
            send_alert("test", "msg")

        mock_client.post.assert_called_once()

    def test_cooldown_blocks_duplicate_alert(self, monkeypatch):
        """A second call with the same alert_type within cooldown is suppressed."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setattr(_alerting, "COOLDOWN_S", 3600.0)
        mock_client = _make_mock_client()

        with patch("services.alerting.httpx.Client", return_value=mock_client), \
             patch("services.alerting.threading.Thread", side_effect=_make_sync_thread):
            send_alert("dup", "first")
            send_alert("dup", "second")

        assert mock_client.post.call_count == 1

    def test_different_alert_types_independent_cooldown(self, monkeypatch):
        """Different alert_types each have their own cooldown entry."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setattr(_alerting, "COOLDOWN_S", 3600.0)
        mock_client = _make_mock_client()

        with patch("services.alerting.httpx.Client", return_value=mock_client), \
             patch("services.alerting.threading.Thread", side_effect=_make_sync_thread):
            send_alert("type_a", "msg")
            send_alert("type_b", "msg")

        assert mock_client.post.call_count == 2

    def test_webhook_error_does_not_propagate(self, monkeypatch):
        """A network exception inside _fire() must not surface from send_alert."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client(raise_exc=Exception("network error"))

        with patch("services.alerting.httpx.Client", return_value=mock_client), \
             patch("services.alerting.threading.Thread", side_effect=_make_sync_thread):
            send_alert("err", "msg")  # must not raise

    def test_webhook_4xx_response_does_not_raise(self, monkeypatch):
        """A 4xx HTTP response is logged but must not raise from send_alert."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client(status_code=400)

        with patch("services.alerting.httpx.Client", return_value=mock_client), \
             patch("services.alerting.threading.Thread", side_effect=_make_sync_thread):
            send_alert("4xx", "msg")  # must not raise

    def test_is_enabled_true_when_url_set(self, monkeypatch):
        """is_enabled() returns True when WEBHOOK_URL is non-empty."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "http://x")
        assert is_enabled() is True

    def test_is_enabled_false_when_url_empty(self, monkeypatch):
        """is_enabled() returns False when WEBHOOK_URL is empty."""
        monkeypatch.setattr(_alerting, "WEBHOOK_URL", "")
        assert is_enabled() is False
