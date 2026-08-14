"""Unit tests for the webhook alerting helper in services/alerting.py.

The module reads ALERT_WEBHOOK_URL, ALERT_COOLDOWN_S, ALERT_WEBHOOK_AUTH,
ALERT_WEBHOOK_FORMAT and ALERT_ENVIRONMENT from the environment on each call
rather than once at import (see the module docstring for why), so these tests
drive it through monkeypatch.setenv/delenv rather than patching module
attributes.
"""

import logging
import os
from unittest.mock import ANY, MagicMock, patch

import pytest

import services.alerting as _alerting
from services.alerting import is_enabled, send_alert


@pytest.fixture(autouse=True)
def _clean_alert_env(monkeypatch):
    """Start every test with all settings unset, whatever the ambient shell
    or .env holds, so each test's setenv/delenv calls are the only source of
    truth for what the module sees. ALERT_ENVIRONMENT is included even
    though it is not an ALERT_WEBHOOK_* setting: send_alert() reads it too,
    and it must not leak in from the shell running the tests."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_COOLDOWN_S", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_AUTH", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    monkeypatch.delenv("ALERT_ENVIRONMENT", raising=False)


@pytest.fixture(autouse=True)
def _fixed_hostname(monkeypatch):
    """Pin socket.gethostname() to a fixed value for every test, so
    assertions on the posted body do not depend on the machine running them
    (per the brief: patch gethostname rather than assert against the real
    name). Tests exercising the gethostname()-raises fallback override this
    within their own patch.
    """
    monkeypatch.setattr(_alerting.socket, "gethostname", lambda: "test-host")


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


def _make_sync_thread(*env_vars):
    """Build a Thread replacement that calls target() synchronously on
    .start(), first removing any named environment variables.

    With no arguments this is a plain synchronous stand-in for
    threading.Thread. With names given, it simulates the environment
    changing between send_alert() capturing a setting and the (normally
    later, real) thread run, proving _fire (and whatever send_alert
    computed before creating it) closes over a value captured at call time
    rather than re-reading os.environ when the thread actually runs.
    Parameterised on the variable name(s) so the same stub covers
    ALERT_WEBHOOK_URL, ALERT_WEBHOOK_FORMAT and ALERT_WEBHOOK_AUTH.
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

    def test_cooldown_blocks_duplicate_alert(self, monkeypatch):
        """A second call with the same alert_type within cooldown is
        suppressed after a *successful* first send: the regression guard for
        the whole cooldown-release change in finding 3.  Asserts the single
        call that did go out, not just its count, so a mutation that lets
        the second call through with a mangled first payload cannot pass.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(status_code=200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("dup", "first")
            send_alert("dup", "second")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "dup",
                "message": "first",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
        )

    def test_zero_cooldown_disables_suppression(self, monkeypatch):
        """A cooldown of 0 means the second call is never suppressed."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "0")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
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
        """A malformed ALERT_COOLDOWN_S must be logged as a warning naming
        the bad value, not swallowed silently."""
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

    def test_whitespace_only_cooldown_treated_as_unset_and_does_not_warn(self, monkeypatch, caplog):
        """ALERT_COOLDOWN_S="   " (whitespace only) must be treated as
        unset, defaulting to 300.0 quietly. A padded numeric value cannot
        pin the strip: float() already tolerates surrounding whitespace, so
        e.g. " 3600 " parses the same whether or not _cooldown_s() strips
        it first. Whitespace-only does discriminate: unstripped, the "not
        raw" check sees a non-empty string, falls through to float("   "),
        which raises, and a spurious "malformed ALERT_COOLDOWN_S" warning
        is logged before the same 300.0 fallback. Asserting the return
        value alone would pass on that spurious-warning path too, so the
        no-warning assertion is the one that actually catches it.
        """
        monkeypatch.setenv("ALERT_COOLDOWN_S", "   ")
        with caplog.at_level(logging.WARNING):
            result = _alerting._cooldown_s()

        assert result == 300.0
        assert caplog.text == ""

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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.WARNING),
        ):
            send_alert("test", "msg")  # must not raise

        assert "bogus_format" in caplog.text
        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
        )

    def test_different_alert_types_independent_cooldown(self, monkeypatch):
        """Different alert_types each have their own cooldown entry."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("err", "msg")  # must not raise

        mock_client.post.assert_called_once()

    def test_webhook_4xx_response_does_not_raise(self, monkeypatch):
        """A 4xx HTTP response is logged but must not raise from send_alert."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client(status_code=400)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
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

    def test_raw_format_default_matches_current_payload_exactly(self, monkeypatch):
        """ALERT_WEBHOOK_FORMAT unset must default to raw and produce exactly
        today's payload shape, with no headers kwarg when ALERT_WEBHOOK_AUTH
        is unset. Pins the body for every pre-existing webhook sink relying
        on this shape and call signature.

        NOTE: this test was named test_raw_format_default_matches_historical_
        payload_exactly and pinned the raw body byte-for-byte identical to
        its shape for the whole branch, up to this commit. This commit adds
        "environment" and "host" to that body deliberately (see
        docs/alerting.md and the module docstring), so the pin below and the
        name were both updated to match. This is not a weakened or deleted
        assertion: it is still a full-dict pin, just of the new shape. Do
        not read this diff as a regression.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg", {"node_id": "n1"})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {"node_id": "n1"},
            },
        )
        assert "headers" not in mock_client.post.call_args.kwargs


class TestClickupChatFormat:
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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
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
        Pins the exact rendering for the no-meta case, which now includes
        the environment and host lines added by this commit.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg", {})

        content = mock_client.post.call_args.kwargs["json"]["content"]
        assert content == "**test**\nmsg\nenvironment: unknown\nhost: test-host"

    def test_content_includes_environment_and_host_lines(self, monkeypatch):
        """environment and host are rendered as their own key: value lines,
        the same shape as a meta entry, ahead of meta itself. This is the
        change this commit makes: the ClickUp channel an alert lands in is
        no longer the only clue to which box raised it.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        monkeypatch.setenv("ALERT_ENVIRONMENT", "production")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg", {"node_id": "n1"})

        content = mock_client.post.call_args.kwargs["json"]["content"]
        assert content == "**test**\nmsg\nenvironment: production\nhost: test-host\nnode_id: n1"

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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg", {})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "type": "message",
                "content": "**test**\nmsg\nenvironment: unknown\nhost: test-host",
            },
            headers={"Authorization": "pk_test_notreal"},
        )


class TestCallTimeCapture:
    """ALERT_WEBHOOK_URL, ALERT_WEBHOOK_FORMAT and ALERT_WEBHOOK_AUTH must
    each be read once, at send_alert() call time, and closed over by _fire,
    not re-read from os.environ when the thread actually runs. A plain
    synchronous stub (_make_sync_thread() with no arguments) runs the
    closure inline, at the same moment as the rest of send_alert, so it
    cannot tell capture from re-read: whichever way the code is written,
    the environment has not changed by the time _fire executes. These tests
    instead pass the setting's name to _make_sync_thread so it is popped
    from the environment just before _fire runs, so that a re-read
    regression has an environment to be caught re-reading from.
    """

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
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread("ALERT_WEBHOOK_URL")),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
        )

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
                side_effect=_make_sync_thread("ALERT_WEBHOOK_FORMAT"),
            ),
        ):
            send_alert("test", "msg", {})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "type": "message",
                "content": "**test**\nmsg\nenvironment: unknown\nhost: test-host",
            },
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
                side_effect=_make_sync_thread("ALERT_WEBHOOK_AUTH"),
            ),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
            headers={"Authorization": "pk_test_notreal"},
        )

    def test_environment_captured_once_not_reread_when_thread_runs(self, monkeypatch):
        """ALERT_ENVIRONMENT must be captured at send_alert() call time,
        exactly like ALERT_WEBHOOK_URL/FORMAT/AUTH above, and closed over by
        _fire rather than re-read when the thread actually runs. Uses the
        same env-popping thread stub, parameterised on ALERT_ENVIRONMENT
        this time.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_ENVIRONMENT", "staging")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread("ALERT_ENVIRONMENT")),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "staging",
                "host": "test-host",
                "meta": {},
            },
        )

    def test_host_captured_once_not_reread_when_thread_runs(self, monkeypatch):
        """Host must likewise be read once, before the thread starts, not
        re-read inside _fire. socket.gethostname() takes no arguments and is
        deterministic, so the env-popping stub used above cannot distinguish
        capture from re-read for it: there is no environment variable to
        pop. Instead this swaps the mocked hostname's return value between
        send_alert()'s capture point and the (stubbed, synchronous) thread
        run, so a re-read inside _fire has a different value to be caught
        reading.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        hostname_state = {"value": "host-at-call-time"}
        monkeypatch.setattr(_alerting.socket, "gethostname", lambda: hostname_state["value"])
        mock_client = _make_mock_client()

        def _side_effect(**kwargs):
            def _run():
                hostname_state["value"] = "host-at-thread-time"
                kwargs["target"]()

            t = MagicMock()
            t.start.side_effect = _run
            return t

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_side_effect),
        ):
            send_alert("test", "msg")

        assert mock_client.post.call_args.kwargs["json"]["host"] == "host-at-call-time"


class TestEnvironmentAndHost:
    """ALERT_ENVIRONMENT and socket.gethostname() are added to every alert
    so the payload itself says which box raised it: channel routing alone is
    configuration, and a fumbled ALERT_WEBHOOK_URL would otherwise put an
    alert in the wrong channel with nothing in it to reveal that.
    """

    def test_raw_body_reflects_call_time_environment_and_host(self, monkeypatch):
        """Exact-dict assertion with both fields set to distinguishing,
        non-default values, so this cannot pass by coincidence with the
        "unknown" fallback used everywhere else in this file.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_ENVIRONMENT", "staging")
        monkeypatch.setattr(_alerting.socket, "gethostname", lambda: "retina-staging")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg", {"node_id": "n1"})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "staging",
                "host": "retina-staging",
                "meta": {"node_id": "n1"},
            },
        )

    def test_alert_environment_unset_yields_unknown(self, monkeypatch):
        """ALERT_ENVIRONMENT unset must render as the literal "unknown", not
        an omitted field: a box with no environment configured is itself
        worth seeing, and a missing field would read as an oversight rather
        than a fact.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        assert mock_client.post.call_args.kwargs["json"]["environment"] == "unknown"

    def test_alert_environment_empty_yields_unknown(self, monkeypatch):
        """ALERT_ENVIRONMENT="" (an .env line with no value, matching how
        .env.example declares it) must be treated the same as unset, not
        rendered as an empty string.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_ENVIRONMENT", "")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        assert mock_client.post.call_args.kwargs["json"]["environment"] == "unknown"

    def test_alert_environment_stripped(self, monkeypatch):
        """docker-compose's env_file passes .env values literally, so a
        trailing space typed into ALERT_ENVIRONMENT is not stripped before
        the process sees it, matching every other setting in this module.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_ENVIRONMENT", "  staging  ")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        assert mock_client.post.call_args.kwargs["json"]["environment"] == "staging"

    def test_gethostname_raising_falls_back_to_unknown_and_does_not_propagate(self, monkeypatch):
        """socket.gethostname() can raise (e.g. OSError from a broken
        resolver). send_alert is itself called from failure-handling paths,
        so that must not turn a reportable problem into a second one: it
        must fall back to "unknown" and send_alert must not raise.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setattr(_alerting.socket, "gethostname", MagicMock(side_effect=OSError("no hostname")))
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")  # must not raise

        assert mock_client.post.call_args.kwargs["json"]["host"] == "unknown"

    def test_gethostname_empty_string_yields_unknown(self, monkeypatch):
        """socket.gethostname() returning "" (no OSError, just an empty
        result) must also fall back to "unknown" rather than posting an
        empty host field: an empty string is as useless as a failure for
        identifying the box.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setattr(_alerting.socket, "gethostname", lambda: "")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        assert mock_client.post.call_args.kwargs["json"]["host"] == "unknown"


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

    def test_unbalanced_ipv6_bracket_does_not_raise(self, monkeypatch, caplog):
        """ALERT_WEBHOOK_URL='http://[::1' makes urlsplit() raise
        ValueError("Invalid IPv6 URL") rather than return an unparsed
        result. main.py calls log_destination() at module scope, before
        uvicorn starts, so an uncaught exception here would take the whole
        server down over a typo in this optional, best-effort setting.
        Falls into the same "does not parse as a URL" branch as any other
        unparseable value.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://[::1")
        with caplog.at_level(logging.INFO):
            _alerting.log_destination()  # must not raise

        assert "does not parse as a URL" in caplog.text


class TestSettingsWhitespaceTolerance:
    """docker-compose's env_file passes .env values literally, unlike a
    shell: a trailing space typed into ALERT_WEBHOOK_FORMAT, ALERT_WEBHOOK_AUTH
    or ALERT_WEBHOOK_URL is not stripped before the process sees it. Each of
    the four settings read by this module must tolerate that.
    """

    def test_webhook_url_strips_surrounding_whitespace(self, monkeypatch):
        """A padded ALERT_WEBHOOK_URL must still be posted to the trimmed
        target, not a URL corrupted by the stray whitespace.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "  http://test-hook/alert  ")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
        )

    def test_cooldown_strips_surrounding_whitespace(self, monkeypatch):
        """A padded ALERT_COOLDOWN_S must parse to the same float as the
        unpadded value. float() already tolerates surrounding whitespace, so
        this is a consistency pin alongside the other three settings rather
        than a behaviour change on its own.
        """
        monkeypatch.setenv("ALERT_COOLDOWN_S", "  3600  ")
        assert _alerting._cooldown_s() == 3600.0

    def test_webhook_format_strips_whitespace_and_uses_clickup_body(self, monkeypatch, caplog):
        """ALERT_WEBHOOK_FORMAT=" clickup_chat " (padded) must still be
        recognised as clickup_chat: it must not fail the `not in _FORMATS`
        check, warn, and silently fall back to posting a raw body that
        ClickUp rejects with 400. Asserts the full call, not a count, and
        that no fallback warning was logged.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", " clickup_chat ")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.WARNING),
        ):
            send_alert("test", "msg", {})

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "type": "message",
                "content": "**test**\nmsg\nenvironment: unknown\nhost: test-host",
            },
        )
        assert "unrecognised" not in caplog.text

    def test_webhook_auth_strips_surrounding_whitespace(self, monkeypatch):
        """A padded ALERT_WEBHOOK_AUTH must be sent as exactly the trimmed
        token: ClickUp 401s on anything else, including a token with a
        trailing space still attached.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", " pk_test_notreal ")
        mock_client = _make_mock_client()

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("test", "msg")

        mock_client.post.assert_called_once_with(
            "http://test-hook/alert",
            json={
                "alert_type": "test",
                "message": "msg",
                "timestamp": ANY,
                "environment": "unknown",
                "host": "test-host",
                "meta": {},
            },
            headers={"Authorization": "pk_test_notreal"},
        )


class TestLogDestinationClickupAuthWarning:
    """log_destination() must not call a dead configuration healthy: the
    clickup_chat format with no ALERT_WEBHOOK_AUTH set 401s on every alert,
    since ClickUp requires the header, so the startup log must warn about
    that specific combination rather than only logging the destination.
    """

    def test_warns_when_clickup_chat_format_and_auth_empty(self, monkeypatch, caplog):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        with caplog.at_level(logging.WARNING):
            _alerting.log_destination()

        assert "ALERT_WEBHOOK_FORMAT" in caplog.text
        assert "ALERT_WEBHOOK_AUTH" in caplog.text

    def test_warns_when_url_unparseable_and_clickup_chat_auth_empty(self, monkeypatch, caplog):
        """The warning must fire regardless of which INFO branch produced
        the line above it. Every other test in this class uses a URL that
        parses, so log_destination() takes the else: branch each time and
        cannot tell whether the warning sits inside that branch or after
        it: a warning block moved inside else: would pass all of them. This
        uses an unparseable ALERT_WEBHOOK_URL, taking the if: branch
        instead, so the warning must be reached from there too.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "notaurl")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        with caplog.at_level(logging.WARNING):
            _alerting.log_destination()

        assert "ALERT_WEBHOOK_FORMAT" in caplog.text
        assert "ALERT_WEBHOOK_AUTH" in caplog.text

    def test_no_warning_when_clickup_chat_format_and_auth_set(self, monkeypatch, caplog):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "clickup_chat")
        monkeypatch.setenv("ALERT_WEBHOOK_AUTH", "pk_test_notreal")
        with caplog.at_level(logging.WARNING):
            _alerting.log_destination()

        assert caplog.text == ""

    def test_no_warning_when_raw_format_and_auth_empty(self, monkeypatch, caplog):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        with caplog.at_level(logging.WARNING):
            _alerting.log_destination()

        assert caplog.text == ""


class TestCooldownReleaseOnFailure:
    """A failed delivery must not consume the cooldown slot it reserved:
    otherwise one failed POST buys a full ALERT_COOLDOWN_S of silence on
    exactly the alert type that just failed to deliver.
    """

    def test_4xx_response_releases_cooldown_for_immediate_retry(self, monkeypatch):
        """No prior send for this alert_type, so there is no previous value
        to restore: the release must remove the key entirely, and the next
        send_alert for the same type must attempt a second POST rather than
        being suppressed.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(status_code=401)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "first")
            send_alert("mender_unreachable", "second")

        assert mock_client.post.call_count == 2
        assert "mender_unreachable" not in _alerting._last_sent

    def test_exception_releases_cooldown_for_immediate_retry(self, monkeypatch):
        """A network exception (not just a 4xx response) must release the
        cooldown too, so a following send for the same alert_type retries
        rather than being suppressed for the full cooldown window.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(raise_exc=Exception("network error"))

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "first")
            send_alert("mender_unreachable", "second")

        assert mock_client.post.call_count == 2
        assert "mender_unreachable" not in _alerting._last_sent

    def test_failed_release_restores_previous_timestamp_rather_than_deleting(self, monkeypatch):
        """When a prior send already occupied the slot, releasing on
        failure must put that previous timestamp back, not just delete the
        key: the brief's compare-and-restore rule, exercised on the "there
        was a previous value" branch. The previous timestamp is set far
        enough in the past that this send is not itself suppressed by
        cooldown.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        _alerting._last_sent["registration_held"] = 1000.0
        mock_client = _make_mock_client(status_code=400)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert _alerting._last_sent["registration_held"] == 1000.0

    def test_failed_release_does_not_clobber_slot_claimed_by_later_send(self, monkeypatch):
        """Compare-and-restore: if a later send has already claimed the slot
        by the time an earlier, failing send tries to release it, the
        earlier release must leave the newer value alone. Exercised
        deterministically (no real threads) by having the mocked POST itself
        overwrite _last_sent before raising, simulating a second caller
        claiming the slot while the first call's request is in flight.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        newer_claim = 99999999999.0

        def _claim_slot_then_fail(*args, **kwargs):
            _alerting._last_sent["race"] = newer_claim
            raise Exception("network error")

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _claim_slot_then_fail

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("race", "msg")  # must not raise, must not clobber the newer claim

        assert _alerting._last_sent["race"] == newer_claim
