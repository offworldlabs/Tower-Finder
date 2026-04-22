"""Unit tests for the webhook alerting helper in services/alerting.py.

The module uses module-level globals (WEBHOOK_URL, _last_sent). These are
monkey-patched per test to keep them isolated.
"""

import os
import time

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import alerting  # noqa: E402


class TestIsEnabled:
    def test_disabled_when_empty(self, monkeypatch):
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "")
        assert alerting.is_enabled() is False

    def test_enabled_when_set(self, monkeypatch):
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "https://example.com/hook")
        assert alerting.is_enabled() is True


class TestSendAlert:
    def test_no_op_when_disabled(self, monkeypatch):
        """send_alert is a silent no-op when WEBHOOK_URL is not set."""
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "")
        called: dict = {}
        original_thread = alerting.threading.Thread

        class _RecordThread(original_thread):
            def __init__(self, *a, **kw):
                called["started"] = True
                super().__init__(*a, **kw)

        monkeypatch.setattr(alerting.threading, "Thread", _RecordThread)
        alerting.send_alert("test_type", "msg", {"k": "v"})
        assert "started" not in called

    def test_cooldown_suppresses_duplicates(self, monkeypatch):
        """Within COOLDOWN_S, the same alert_type is not re-sent."""
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "https://example.com/hook")
        # Fresh cooldown dict for this test
        monkeypatch.setattr(alerting, "_last_sent", {})
        # Long cooldown so the second call is blocked
        monkeypatch.setattr(alerting, "COOLDOWN_S", 3600.0)

        starts: list[dict] = []

        class _FakeThread:
            def __init__(self, *a, **kw):
                starts.append({"target": kw.get("target")})
                self._target = kw.get("target")

            def start(self):
                # Don't actually fire the webhook — just record the start call.
                starts[-1]["started"] = True

        monkeypatch.setattr(alerting.threading, "Thread", _FakeThread)

        alerting.send_alert("dup_type", "first")
        alerting.send_alert("dup_type", "second")  # should be suppressed

        started_count = sum(1 for s in starts if s.get("started"))
        assert started_count == 1, f"expected exactly 1 webhook start, got {started_count}"

    def test_different_types_not_suppressed(self, monkeypatch):
        """Different alert_types bypass the cooldown for each other."""
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "https://example.com/hook")
        monkeypatch.setattr(alerting, "_last_sent", {})
        monkeypatch.setattr(alerting, "COOLDOWN_S", 3600.0)

        starts: list = []

        class _FakeThread:
            def __init__(self, *a, **kw):
                starts.append(kw.get("target"))

            def start(self):
                pass

        monkeypatch.setattr(alerting.threading, "Thread", _FakeThread)

        alerting.send_alert("type_a", "a")
        alerting.send_alert("type_b", "b")
        assert len(starts) == 2

    def test_cooldown_expires(self, monkeypatch):
        """After cooldown passes, alerts of the same type fire again."""
        monkeypatch.setattr(alerting, "WEBHOOK_URL", "https://example.com/hook")
        # Pre-populate with a stale timestamp well outside cooldown
        monkeypatch.setattr(alerting, "_last_sent", {"expired": time.time() - 10000.0})
        monkeypatch.setattr(alerting, "COOLDOWN_S", 1.0)

        starts: list = []

        class _FakeThread:
            def __init__(self, *a, **kw):
                starts.append(True)

            def start(self):
                pass

        monkeypatch.setattr(alerting.threading, "Thread", _FakeThread)
        alerting.send_alert("expired", "msg")
        assert len(starts) == 1
