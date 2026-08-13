"""Unit tests for the webhook alerting helper in services/alerting.py.

The module reads ALERT_WEBHOOK_URL, ALERT_COOLDOWN_S, ALERT_WEBHOOK_AUTH and
ALERT_WEBHOOK_FORMAT from the environment on each call rather than once at
import (see the module docstring for why), so these tests drive it through
monkeypatch.setenv/delenv rather than patching module attributes.
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
    """Start every test with all settings unset, whatever the ambient shell
    or .env holds, so each test's setenv/delenv calls are the only source of
    truth for what the module sees."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_COOLDOWN_S", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_AUTH", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)


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


def _make_env_popping_sync_thread(*env_vars):
    """Build a Thread replacement that removes the named environment
    variables before invoking target() synchronously on .start().

    Simulates the environment changing between send_alert() capturing a
    setting and the (normally later, real) thread run. Proves _fire (and
    whatever send_alert computed before creating it) closes over a value
    captured at call time rather than re-reading os.environ when the thread
    actually runs. Parameterised on the variable name(s) so the same stub
    covers ALERT_WEBHOOK_URL, ALERT_WEBHOOK_FORMAT and ALERT_WEBHOOK_AUTH.
    """

    def _side_effect(**kwargs):
        def _run():
            for name in env_vars:
                os.environ.pop(name, None)
            kwargs["target"]()

        t = MagicMock()
        t.start.side_effect = _run
        return t

    return _side_effect


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
            patch("services.alerting.threading.Thread", side_effect=_make_env_popping_sync_thread("ALERT_WEBHOOK_URL")),
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

    def test_raw_format_default_matches_historical_payload_exactly(self, monkeypatch):
        """ALERT_WEBHOOK_FORMAT unset must default to raw and produce exactly
        today's payload shape, with no headers kwarg when ALERT_WEBHOOK_AUTH
        is unset. Pins backward compatibility for every pre-existing webhook
        sink relying on the old body shape and call signature.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg", {"node_id": "n1"})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"alert_type": "test", "message": "msg", "timestamp": ANY, "meta": {"node_id": "n1"}},
        )
        assert "headers" not in mock_client.post.call_args.kwargs


class TestWebhookAuth:
    def test_auth_sent_verbatim_no_bearer_prefix(self, monkeypatch):
        """ALERT_WEBHOOK_AUTH must be sent as-is in the Authorization header:
        a ClickUp personal token carries no "Bearer " prefix, and adding one
        would silently produce a 401.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", "pk_test_notreal")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg")

        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["headers"] == {"Authorization": "pk_test_notreal"}

    def test_empty_auth_sends_no_authorization_header(self, monkeypatch):
        """With ALERT_WEBHOOK_AUTH unset (empty), no Authorization header is
        sent at all, rather than an empty one.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg")

        assert "headers" not in mock_client.post.call_args.kwargs


class TestClickupChatFormat:
    def test_body_has_exactly_type_and_content_keys(self, monkeypatch):
        """clickup_chat must produce exactly {"type": "message", "content":
        ...} with no other keys: not the raw payload shape, and not a
        superset of it.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("mender_unreachable", "node offline", {"node_id": "n1"})

        body = mock_client.post.call_args.kwargs["json"]
        assert set(body.keys()) == {"type", "content"}
        assert body["type"] == "message"

    def test_content_contains_alert_type_message_and_each_meta_pair(self, monkeypatch):
        """The rendered content must carry alert_type, message and every
        meta key/value: meta is what carries the node id, and is the
        difference between an actionable alert and a shrug.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("registration_held", "node stuck", {"node_id": "n42", "reason": "fingerprint mismatch"})

        content = mock_client.post.call_args.kwargs["json"]["content"]
        assert "registration_held" in content
        assert "node stuck" in content
        assert "node_id" in content
        assert "n42" in content
        assert "reason" in content
        assert "fingerprint mismatch" in content

    def test_empty_meta_renders_cleanly(self, monkeypatch):
        """meta={} must not leave a dangling heading or trailing whitespace.
        Pins the exact rendering for the no-meta case.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg", {})

        content = mock_client.post.call_args.kwargs["json"]["content"]
        assert content == "**test**\nmsg"
        assert content == content.rstrip()

    def test_unrecognised_format_warns_and_falls_back_to_raw(self, monkeypatch, caplog):
        """An unrecognised ALERT_WEBHOOK_FORMAT logs a warning naming it and
        falls back to raw, mirroring _cooldown_s()'s handling of a malformed
        ALERT_COOLDOWN_S. It must not raise: send_alert runs on failure
        paths and must never turn a reportable problem into a second one.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "bogus_format")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
            caplog.at_level(logging.WARNING),
        ):
            send_alert("test", "msg")  # must not raise

        assert "bogus_format" in caplog.text
        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"alert_type": "test", "message": "msg", "timestamp": ANY, "meta": {}},
        )

    def test_full_call_pins_url_headers_and_body_together(self, monkeypatch):
        """None of the tests above assert the URL or the headers on the
        clickup_chat path: they only inspect mock_client.post.call_args.kwargs,
        so a mutation that posts this format's body to the wrong URL, or that
        drops the Authorization header on this branch only, would pass all of
        them. The header drop is the real-world failure: ClickUp returns 401
        without it, and the alert disappears behind the existing >= 400
        warning with nothing else to show for it. This test pins the whole
        call (URL, headers and body) in one assertion, with
        ALERT_WEBHOOK_FORMAT and ALERT_WEBHOOK_AUTH both set, so it fails if
        either regresses.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", "pk_test_notreal")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread),
        ):
            send_alert("test", "msg", {})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"type": "message", "content": "**test**\nmsg"},
            headers={"Authorization": "pk_test_notreal"},
        )


class TestCallTimeCapture:
    """ALERT_WEBHOOK_FORMAT and ALERT_WEBHOOK_AUTH must be read once, at
    send_alert() call time, and closed over by _fire, not re-read from
    os.environ when the thread actually runs. _make_sync_thread runs the
    closure inline, at the same moment as the rest of send_alert, so it
    cannot tell capture from re-read for these two settings: whichever way
    the code is written, the environment has not changed by the time _fire
    executes. These tests use _make_env_popping_sync_thread instead, exactly
    as test_url_captured_once_not_reread_when_thread_runs already does for
    ALERT_WEBHOOK_URL, so that a re-read regression has an environment to be
    caught re-reading from.
    """

    def test_format_captured_once_not_reread_when_thread_runs(self, monkeypatch):
        """ALERT_WEBHOOK_FORMAT set to clickup_chat must still produce the
        clickup_chat body even if the setting is removed from the
        environment before the thread runs. Guards against the format
        check moving inside _fire and re-reading os.environ there, which
        would fall back to raw for any alert that happens to fire after the
        environment changes.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch(
                "services.alerting.threading.Thread",
                side_effect=_make_env_popping_sync_thread("ALERT_WEBHOOK_FORMAT"),
            ),
        ):
            send_alert("test", "msg", {})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"type": "message", "content": "**test**\nmsg"},
        )

    def test_auth_captured_once_not_reread_when_thread_runs(self, monkeypatch):
        """ALERT_WEBHOOK_AUTH set must still produce the Authorization header
        even if the setting is removed from the environment before the
        thread runs. Guards against the header build moving inside _fire and
        re-reading os.environ there, which would silently stop sending the
        header for any alert that happens to fire after the environment
        changes: the same real-world failure as the wrong-URL / dropped-
        header case above, reached from the other end.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", "pk_test_notreal")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch(
                "services.alerting.threading.Thread",
                side_effect=_make_env_popping_sync_thread("ALERT_WEBHOOK_AUTH"),
            ),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={"alert_type": "test", "message": "msg", "timestamp": ANY, "meta": {}},
            headers={"Authorization": "pk_test_notreal"},
        )


class TestLogDestination:
    def test_logs_scheme_and_host_without_leaking_token_or_full_url(self, monkeypatch, caplog):
        """The startup log must name the destination (scheme + host) but
        never the Authorization value, and never the full URL: for the
        ClickUp chat endpoint that carries workspace and channel ids in its
        path.
        """
        monkeypatch.setenv(
            "ALERT_WEBHOOK_URL",
            "https://api.clickup.com/api/v3/workspaces/90152460893/chat/channels/6-901522086236-8/messages",
        )
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", "pk_test_notreal")
        with caplog.at_level(logging.INFO):
            _alerting.log_destination()

        assert "api.clickup.com" in caplog.text
        assert "https" in caplog.text
        assert "pk_test_notreal" not in caplog.text
        assert "90152460893" not in caplog.text
        assert "chat/channels" not in caplog.text

    def test_logs_disabled_when_url_unset(self, caplog):
        """With ALERT_WEBHOOK_URL unset, the startup log says alerting is
        disabled rather than naming a destination.
        """
        with caplog.at_level(logging.INFO):
            _alerting.log_destination()

        assert "disabled" in caplog.text.lower()

    def test_logs_unparseable_url_without_a_bare_scheme_and_host(self, monkeypatch, caplog):
        """A malformed ALERT_WEBHOOK_URL (no scheme, e.g. a value set
        without a scheme by mistake) must not log "://None": that leaks
        nothing sensitive but is meaningless to an operator reading the
        startup log.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "notaurl")
        with caplog.at_level(logging.INFO):
            _alerting.log_destination()

        assert "://None" not in caplog.text
        assert "://" not in caplog.text
