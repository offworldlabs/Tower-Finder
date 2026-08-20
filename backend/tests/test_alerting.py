"""Unit tests for the webhook alerting helper in services/alerting.py.

The module reads ALERT_WEBHOOK_URL, ALERT_COOLDOWN_S, ALERT_WEBHOOK_AUTH,
ALERT_WEBHOOK_FORMAT and ALERT_ENVIRONMENT from the environment on each call
rather than once at import (see the module docstring for why), so these tests
drive it through monkeypatch.setenv/delenv rather than patching module
attributes.
"""

import logging
import os
import time
from unittest.mock import ANY, MagicMock, patch

import httpx
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


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record the backoff delays instead of sleeping them, and hand the
    record to any test that wants to assert on the schedule.

    Autouse so that no test can spend a real backoff by forgetting to ask
    for it; requested by name where the delays themselves are the subject.
    Patches services.alerting._sleep rather than time.sleep, so an unrelated
    caller's sleep can never be mistaken for one of ours.
    """
    delays = []
    monkeypatch.setattr(_alerting, "_sleep", delays.append)
    return delays


def _expected_backoff_window(attempt):
    """The window services.alerting draws attempt `attempt`'s delay from.

    Recomputed here rather than imported, so that the production module owes
    the tests no seam of its own and a change to the schedule has to be made
    deliberately in both places.
    """
    return _alerting._BACKOFF_BASE_S * (2 ** (attempt - 1))


def _make_mock_client(status_code=200, raise_exc=None):
    """Build a context-manager-compatible httpx.Client mock."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    # Real httpx.Headers, as the production call site sees: a MagicMock
    # would hand back a truthy stand-in that only fails to parse by
    # accident, and a plain dict would not model the case-insensitive
    # lookup that lets a sink answer with a lowercased retry-after.
    mock_resp.headers = httpx.Headers()

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


class TestCooldownAfterFailedDelivery:
    """What a failed delivery does to the cooldown slot it reserved.

    The rule used to be "always release", on the reasoning that one failed
    POST must not buy a full ALERT_COOLDOWN_S of silence on exactly the
    alert type that failed to deliver. That predates delivery retries, and
    on its own it put a failing sink on the health monitor's 30s cycle:
    retried more often than a healthy one, which is how a 500 problem
    becomes a 429 problem (ClickUp 86cb5cuxr).

    The reservation now turns on whether the sink can recover from the
    failure. These tests pin both halves of that split; none of them is a
    weakened assertion of the old rule.
    """

    def test_terminal_4xx_keeps_its_cooldown_slot(self, monkeypatch):
        """A 401 is a bad token, so every send until an operator acts would
        fail identically. Keeping the reservation reports it once per
        cooldown rather than once per health cycle, which is the difference
        between a signal and the noise this channel is being fixed to stop
        carrying.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(status_code=401)

        before = time.time()
        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "first")
            send_alert("mender_unreachable", "second")

        mock_client.post.assert_called_once()
        # The full reservation, not a backdated one: asserting only that the
        # key is present would pass just as well if the slot had been
        # reopened, since _FAILURE_REOPEN_S still sits inside this cooldown.
        assert before <= _alerting._last_sent["mender_unreachable"] <= time.time()

    def test_exhausted_retriable_failure_reopens_the_slot_early(self, monkeypatch):
        """A sink that 500s may well be back before the window is out, so
        the reservation gives way to an explicit deadline _FAILURE_REOPEN_S
        out: far short of a full cooldown of silence on an alert that never
        arrived, and far longer than the health monitor's cycle.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(status_code=500)

        before = time.time()
        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "first")

        assert "mender_unreachable" not in _alerting._last_sent
        reopen_at = _alerting._reopen_at["mender_unreachable"]
        assert before + _alerting._FAILURE_REOPEN_S <= reopen_at
        assert reopen_at <= time.time() + _alerting._FAILURE_REOPEN_S

    def test_the_reopen_deadline_outranks_a_shorter_cooldown(self, monkeypatch):
        """The deadline is a floor on how soon the next send may go, and it
        has to hold even when ALERT_COOLDOWN_S is shorter than it.

        This reverses what this test asserted when the deadline was
        implemented by backdating the reservation, where a short cooldown
        won and the failed type was retried on the operator's schedule.
        That is the amplification case: at a 5s cooldown it would put
        _MAX_DELIVERY_ATTEMPTS posts every 5s onto a sink that is already
        failing. Sparing the sink is the whole point, so the floor wins.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "5")
        mock_client = _make_mock_client(status_code=500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "first")
            send_alert("registration_held", "second")

        assert mock_client.post.call_count == _alerting._MAX_DELIVERY_ATTEMPTS
        assert _alerting._reopen_at["registration_held"] >= time.time()

    def test_the_reopen_deadline_survives_a_cooldown_change(self, monkeypatch):
        """The deadline is absolute, not a window back-computed from the
        cooldown in force when the delivery failed. Settings are read afresh
        on every call, so a backdated reservation would be reinterpreted
        against whatever ALERT_COOLDOWN_S the next send happens to read: drop
        an hour-long cooldown to 30s after a failure stamped against it and
        the alert would stay suppressed for the rest of the hour.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_mock_client(status_code=500)

        before = time.time()
        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "first")

        monkeypatch.setenv("ALERT_COOLDOWN_S", "30")
        # _FAILURE_REOPEN_S out from the send, not some fraction of the 3600s
        # window that happened to be configured when it failed.
        reopen_at = _alerting._reopen_at["registration_held"]
        assert before + _alerting._FAILURE_REOPEN_S <= reopen_at
        assert reopen_at <= time.time() + _alerting._FAILURE_REOPEN_S

    def test_failed_release_does_not_clobber_slot_claimed_by_later_send(self, monkeypatch):
        """A failing send must leave a slot it does not own alone, rather
        than reopening someone else's reservation.

        The in-flight guard should stop a second send ever claiming the slot
        mid-delivery, so this drives the state directly rather than through
        send_alert: it pins the reopen's own precondition, which has to hold
        whatever _in_flight is doing, and not the reachability of the race
        through today's call path.
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


def _make_sequenced_mock_client(*outcomes, response_headers=None):
    """Build an httpx.Client mock whose successive post() calls follow
    `outcomes`: an int is posted back as that status code, an exception
    instance is raised instead.

    The last outcome repeats once the sequence is exhausted, so a test that
    wants "fails every time" can pass a single outcome and then assert on
    the attempt count rather than having to predict it up front.

    `response_headers` is carried on every response, for the tests that
    drive Retry-After.
    """
    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)

    attempts = {"n": 0}

    def _post(*args, **kwargs):
        outcome = outcomes[min(attempts["n"], len(outcomes) - 1)]
        attempts["n"] += 1
        if isinstance(outcome, BaseException):
            raise outcome
        resp = MagicMock()
        resp.status_code = outcome
        resp.headers = httpx.Headers(response_headers or {})
        return resp

    mock_client.post.side_effect = _post
    return mock_client


class TestDeliveryRetry:
    """ClickUp's chat API returns intermittent 500s (measured at roughly one
    delivery in three on production, with no 429s anywhere), and a single
    POST per alert means that share of alerts is simply lost.

    That is survivable for the health monitor, whose conditions are still
    true at the next cycle, but not for `mender_unreachable` or
    `registration_held`: both fire once, at the moment they matter. See
    ClickUp 86cb5cuxr.
    """

    def test_500_is_retried_and_the_alert_arrives(self, monkeypatch):
        """The headline case: a 500 on the first attempt must not lose the
        alert. A second attempt succeeds, so exactly two POSTs are made.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500, 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "node taken over")

        assert mock_client.post.call_count == 2

    def test_delivery_that_eventually_succeeds_keeps_its_cooldown_slot(self, monkeypatch):
        """A retried-then-delivered alert has arrived, so it must consume
        its cooldown like any other successful send rather than leaving the
        slot open for an immediate duplicate.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_sequenced_mock_client(500, 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "node taken over")

        assert "registration_held" in _alerting._last_sent

    def test_connection_error_is_retried_and_the_alert_arrives(self, monkeypatch):
        """Connection-level failures are retriable for the same reason a
        5xx is: the request never reached a handler that made a decision.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(httpx.ConnectError("refused"), 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "enrolment down")

        assert mock_client.post.call_count == 2

    def test_read_timeout_is_retried(self, monkeypatch):
        """Timeouts are httpx.RequestError subclasses too, and are the other
        half of what "connection errors" means in practice.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(httpx.ReadTimeout("slow"), 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("mender_unreachable", "enrolment down")

        assert mock_client.post.call_count == 2

    def test_retrying_is_bounded(self, monkeypatch):
        """A sink that is down must not be hammered indefinitely by a
        fire-and-forget thread: the attempts stop at the module's bound.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert mock_client.post.call_count == _alerting._MAX_DELIVERY_ATTEMPTS

    def test_401_is_not_retried(self, monkeypatch):
        """A 401 is a bad token. Every attempt would fail identically, so
        retrying only multiplies the same failure against the sink.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(401)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        mock_client.post.assert_called_once()

    def test_400_is_not_retried(self, monkeypatch):
        """A 400 is a bad payload: the same body will be rejected the same
        way on every attempt.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(400)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        mock_client.post.assert_called_once()

    def test_unexpected_exception_is_not_retried(self, monkeypatch):
        """Only transport failures are retriable. An error raised from
        anywhere else in _fire (a serialisation bug, say) is deterministic,
        so retrying it just repeats the bug.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(RuntimeError("bug"))

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")  # must not raise

        mock_client.post.assert_called_once()

    def test_cooldown_is_held_across_retries_and_reopened_once_at_the_end(self, monkeypatch):
        """The reservation must survive the retry sequence rather than being
        dropped per failed attempt: reopening early would let the next
        health cycle start a second delivery for the same alert while this
        one is still retrying, which is how a 500 problem becomes a 429
        problem. Each attempt records whether the slot was still claimed
        when it ran; the reopen happens only after the last one.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        claimed_during_attempt = []

        def _post(*args, **kwargs):
            claimed_during_attempt.append("registration_held" in _alerting._last_sent)
            resp = MagicMock()
            resp.status_code = 500
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert claimed_during_attempt == [True] * _alerting._MAX_DELIVERY_ATTEMPTS
        # Reopened only once the attempts were spent: the reservation gives
        # way to an explicit deadline _FAILURE_REOPEN_S out.
        assert "registration_held" not in _alerting._last_sent
        assert _alerting._reopen_at["registration_held"] <= time.time() + _alerting._FAILURE_REOPEN_S

    def test_exhausted_delivery_is_logged_at_error(self, monkeypatch, caplog):
        """An alert that never arrived is precisely the event nobody is
        watching for, so it must not be filed at the same level as the
        individual retriable failures.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "registration_held" in errors[0].getMessage()

    def test_non_retriable_failure_is_logged_at_error(self, monkeypatch, caplog):
        """A 401 drops the alert just as completely as an exhausted retry
        sequence does, and is the more urgent of the two to notice.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(401)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "401" in errors[0].getMessage()

    def test_delivery_that_succeeds_on_retry_is_not_logged_at_error(self, monkeypatch, caplog):
        """A recovered delivery is not a failure. Logging it at error would
        put the noise back that the retry exists to remove.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500, 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_backoff_is_slept_between_attempts_but_not_after_the_last(self, monkeypatch, slept):
        """Backoff separates the attempts; sleeping after the final one
        would just delay the thread's exit for no benefit.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert len(slept) == _alerting._MAX_DELIVERY_ATTEMPTS - 1

    def test_backoff_delays_fall_inside_their_own_attempt_window(self, monkeypatch, slept):
        """Each delay is drawn from the upper half of that attempt's window,
        so jitter can produce neither a zero wait (which would make the
        retries a burst) nor one longer than the schedule intends.

        Pinned against the window the delay was drawn from rather than
        against a single ceiling constant: a ceiling wide enough to cover
        the last attempt says nothing about the first, and would sit
        unchanged while a raised _BACKOFF_BASE_S quietly multiplied every
        wait.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        windows = [_expected_backoff_window(attempt) for attempt in range(1, _alerting._MAX_DELIVERY_ATTEMPTS)]
        assert len(slept) == len(windows)
        assert all(window / 2 <= delay <= window for delay, window in zip(slept, windows))

    def test_backoff_is_jittered_rather_than_a_fixed_schedule(self, monkeypatch, slept):
        """Every box running this alerter shares one sink, so a fixed
        schedule would line their retries up into a thundering herd exactly
        when the sink is already struggling.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        # A distinct alert type per send: each failed delivery sets a reopen
        # deadline for its own type, which is the point of that mechanism, so
        # repeating one type here would measure the suppression rather than
        # the jitter.
        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            for i in range(12):
                send_alert(f"registration_held_{i}", "msg")

        first_delays = slept[0 :: _alerting._MAX_DELIVERY_ATTEMPTS - 1]
        assert len(set(first_delays)) > 1

    def test_successful_first_attempt_does_not_sleep(self, monkeypatch, slept):
        """The overwhelmingly common path must be exactly as fast as it was
        before retries existed.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert slept == []


class TestTransient4xxAreRetried:
    """408 and 429 are the two 4xx that clear on their own, so classing every
    4xx as terminal drops exactly the alerts a rate limit was going to let
    through a moment later.

    This matters more once delivery is retried at all: three attempts per
    alert triples the request volume against a sink that is already the
    reason for the retry, which is the "500 problem becomes a 429 problem"
    the ticket named. Mirrors the x-retry taxonomy in
    routes/node_responses.py, where only a refused credential is terminal.
    """

    def test_429_is_retried_and_the_alert_arrives(self, monkeypatch):
        """A rate limit is transient by definition: the sink is telling us
        to come back, not that the request was wrong.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(429, 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "node taken over")

        assert mock_client.post.call_count == 2

    def test_408_is_retried(self, monkeypatch):
        """A request timeout says the sink never got a whole request, so
        the same body may well succeed on a second attempt.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(408, 200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert mock_client.post.call_count == 2

    def test_403_is_still_terminal(self, monkeypatch):
        """Only 408 and 429 are exempt. A 403 is a decision about this
        token, so it must not be dragged into the retriable set with them.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(403)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        mock_client.post.assert_called_once()

    def test_retry_after_is_honoured_over_the_backoff(self, monkeypatch, slept):
        """The sink's own answer to "when should I come back" beats our
        guess. Ignoring it is how a rate-limited client stays rate-limited.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(429, 200, response_headers={"Retry-After": "7"})

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert slept == [7.0]

    def test_retry_after_is_capped(self, monkeypatch, slept):
        """Past _MAX_RETRY_AFTER_S the sink is asking for a wait long enough
        that the alert would arrive stale, and a daemon thread sitting on
        the payload that long is the worse of the two failures.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(429, 200, response_headers={"Retry-After": "3600"})

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert slept == [_alerting._MAX_RETRY_AFTER_S]

    def test_retry_after_never_shortens_the_backoff(self, monkeypatch, slept):
        """A sink that answers "come back in a millisecond" must not be able
        to turn the retry into an immediate second POST.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(429, 200, response_headers={"Retry-After": "0.001"})

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        window = _expected_backoff_window(1)
        assert window / 2 <= slept[0] <= window

    def test_malformed_retry_after_falls_back_to_the_backoff(self, monkeypatch, slept):
        """This runs on the failure path, so an HTTP-date or any other
        unparseable value must not be able to raise out of it and turn a
        retriable failure into a lost alert.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(
            429, 200, response_headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")  # must not raise

        window = _expected_backoff_window(1)
        assert mock_client.post.call_count == 2
        assert window / 2 <= slept[0] <= window


class TestDeliveryRetryDetails:
    """Behaviour of the retry sequence that the headline cases do not pin."""

    def test_a_terminal_4xx_mid_sequence_stops_the_retries(self, monkeypatch):
        """The token expires between attempts: 500 then 401. The terminal
        answer must end the sequence where it arrives rather than the
        earlier 5xx buying the rest of the attempts.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500, 401)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert mock_client.post.call_count == 2

    def test_a_terminal_4xx_mid_sequence_keeps_the_cooldown_slot(self, monkeypatch):
        """Reaching the 401 by way of a 500 must land on the terminal
        cooldown rule, not the reopen one: what matters is how the sequence
        ended, not how it started.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_sequenced_mock_client(500, 401)

        before = time.time()
        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert before <= _alerting._last_sent["registration_held"] <= time.time()

    def test_one_client_serves_the_whole_sequence(self, monkeypatch):
        """Retries against a struggling sink should reuse the connection
        rather than pay for a fresh pool and TLS handshake per attempt.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client) as mock_cls,
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert mock_client.post.call_count == _alerting._MAX_DELIVERY_ATTEMPTS
        mock_cls.assert_called_once()

    def test_terminal_4xx_log_keeps_the_greppable_wording(self, monkeypatch, caplog):
        """ClickUp 86cb5cuxr counted this bug by grepping the droplet logs
        for "Alert webhook returned <code>". A 4xx that drops every alert is
        the last line that should fall out of that pattern.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(401)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        assert "Alert webhook returned 401" in caplog.text

    def test_exhausted_5xx_log_keeps_the_greppable_wording(self, monkeypatch, caplog):
        """Same pattern, on the path the ticket actually measured."""
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(500)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        assert "Alert webhook returned 500" in caplog.text

    def test_unreachable_sink_logs_the_traceback_once_at_the_end(self, monkeypatch, caplog):
        """The exhausted-transport-failure line is the one place the
        exception detail is worth carrying; the retriable attempts before it
        would only repeat it.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(httpx.ConnectError("refused"))

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        with_traceback = [r for r in caplog.records if r.exc_info]
        assert len(with_traceback) == 1
        assert with_traceback[0].levelno >= logging.ERROR


class TestRedirectsAreNotDelivered:
    """httpx is not configured to follow redirects, and deliberately so:
    301/302/303 turn a POST into a GET, so following one would deliver an
    empty request rather than the alert.

    That makes a 3xx a delivery failure, not a success. Treating anything
    under 400 as delivered left a redirecting ALERT_WEBHOOK_URL discarding
    every alert with nothing logged at any level, which is the same silent
    loss the retry work exists to remove.
    """

    def test_a_redirect_is_not_counted_as_delivered(self, monkeypatch, caplog):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(302)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "302" in errors[0].getMessage()

    def test_a_redirect_names_the_url_rather_than_the_token(self, monkeypatch, caplog):
        """The remedy for a redirect is to repoint ALERT_WEBHOOK_URL, so
        sending an operator to check their credentials would be a wrong
        steer on the one line they get.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(302)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        assert "ALERT_WEBHOOK_URL" in caplog.text
        assert "ALERT_WEBHOOK_AUTH" not in caplog.text

    def test_a_redirect_is_not_retried(self, monkeypatch):
        """A redirect is a stable fact about the URL, so every attempt would
        be answered the same way.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(301)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        mock_client.post.assert_called_once()

    def test_a_204_is_still_a_successful_delivery(self, monkeypatch, caplog):
        """The success test narrowed from "under 400" to the 2xx range, so
        the non-200 successes a webhook sink may answer with have to keep
        counting as delivered.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "3600")
        mock_client = _make_sequenced_mock_client(204)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
            caplog.at_level(logging.DEBUG),
        ):
            send_alert("registration_held", "msg")

        mock_client.post.assert_called_once()
        assert caplog.text == ""
        assert "registration_held" in _alerting._last_sent


class TestConcurrentDeliveryIsRefused:
    """A retry sequence can outlast its own cooldown: three 10s timeouts plus
    two Retry-After waits is 150s, against a 300s default that is set per
    droplet and could be lower. The reservation in _last_sent therefore
    cannot by itself stop the next health cycle opening a second, concurrent
    delivery of the same alert, which is the load the retry exists to spare
    a struggling sink.
    """

    def test_a_send_is_refused_while_the_same_type_is_still_in_flight(self, monkeypatch):
        """Driven by having the first delivery's POST call send_alert again,
        standing in for the next health cycle firing mid-sequence. The
        cooldown is zeroed so that it cannot be what does the refusing, and
        no reopen deadline exists yet because the delivery has not finished.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        monkeypatch.setenv("ALERT_COOLDOWN_S", "0")
        attempts = []

        def _post(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                send_alert("registration_held", "the next health cycle")
            resp = MagicMock()
            resp.status_code = 500
            resp.headers = httpx.Headers()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert len(attempts) == _alerting._MAX_DELIVERY_ATTEMPTS

    def test_the_in_flight_marker_is_cleared_once_delivery_ends(self, monkeypatch):
        """Otherwise one delivery silences its alert type for the rest of
        the process's life.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(200)

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert _alerting._in_flight == set()

    def test_the_in_flight_marker_is_cleared_after_a_failed_delivery(self, monkeypatch):
        """The failure path runs through its own error handling and reopen,
        so it is the one most likely to leak the marker.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(RuntimeError("bug"))

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert _alerting._in_flight == set()

    def test_a_different_alert_type_is_not_blocked(self, monkeypatch):
        """The guard is per alert type. registration_held must not be held
        up because some unrelated health alert is mid-retry: it fires once,
        at the moment it matters.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        seen = []

        def _post(*args, **kwargs):
            seen.append(kwargs["json"])
            if len(seen) == 1:
                send_alert("registration_held", "node taken over")
            resp = MagicMock()
            resp.status_code = 500
            resp.headers = httpx.Headers()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("solver_latency_high", "msg")

        assert any(body["alert_type"] == "registration_held" for body in seen)


class TestRetryCostsTheSinkNoMoreThanBefore:
    """The retry must not leave a failing sink under more load than it was
    under before the retry existed, which is the 500-becomes-429 case the
    ticket named (86cb5cuxr).
    """

    def test_the_reopen_window_covers_a_full_sequence_of_attempts(self):
        """Against a dead sink the old code sent one POST per health-monitor
        cycle. The new code sends _MAX_DELIVERY_ATTEMPTS per reopen window,
        so the window has to be at least attempts x cycle for that to be no
        worse. At a 60s window and the 30s default cycle it would be half
        again the old rate, which is why this is pinned rather than left to
        the constants looking reasonable.
        """
        from services.tasks.health_monitor import HEALTH_MONITOR_INTERVAL_S

        old_rate = 1 / HEALTH_MONITOR_INTERVAL_S
        new_rate = _alerting._MAX_DELIVERY_ATTEMPTS / _alerting._FAILURE_REOPEN_S
        assert new_rate <= old_rate

    def test_retry_after_is_honoured_on_a_5xx_too(self, monkeypatch, slept):
        """A 503 during a maintenance window carries Retry-After as readily
        as a 429 does, and the code path is shared, so narrowing the header
        read to the 4xx set would go unnoticed without this.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(503, 200, response_headers={"Retry-After": "9"})

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert slept == [9.0]

    def test_a_lowercased_retry_after_is_honoured(self, monkeypatch, slept):
        """HTTP/2 lowercases header names, and httpx.Headers is
        case-insensitive, so the sink's casing must not decide whether its
        Retry-After is read.
        """
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://test-hook/alert")
        mock_client = _make_sequenced_mock_client(429, 200, response_headers={"retry-after": "8"})

        with (
            patch("services.alerting.httpx.Client", return_value=mock_client),
            patch("services.alerting.threading.Thread", side_effect=_make_sync_thread()),
        ):
            send_alert("registration_held", "msg")

        assert slept == [8.0]
