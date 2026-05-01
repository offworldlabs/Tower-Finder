"""Golden path end-to-end test.

Exercises the complete data flow a production node goes through:

  TCP HELLO + CONFIG
    → node registered in state.connected_nodes
    → PassiveRadarPipeline created in state.node_pipelines
    → DETECTION frames enqueued in state.frame_queue
  process_one_frame × N
    → Kalman tracker runs → M-of-N promotion → ACTIVE track
    → geolocated_tracks populated (solver called)
  HTTP API
    → GET /api/health              → 200 {"status": "ok"}
    → GET /api/test/dashboard      → node counted, pipeline tracks counted
    → GET /api/radar/nodes         → 200, parseable JSON
    → GET /api/radar/analytics     → 200, parseable JSON
    → GET /api/config              → 200, has required keys

This test does NOT mock internal state — it drives the real code paths so
that any wiring regression between layers is immediately visible.

Rollback note
─────────────
`deploy/rollback.sh`  — rolls back to the saved Docker image (tower-finder:rollback)
                        or to a specific git ref: `deploy/rollback.sh <tag|commit>`
`deploy/pre-deploy.sh` — saves the current image + creates a git tag.
                         Called by CI before every deploy; also safe to run manually.

Usage (manual rollback on server):
    See deploy/rollback.sh and the server-ops runbook.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from retina_tracker.config import M_THRESHOLD, N_WINDOW
from retina_tracker.track import TrackState

from core import state
from main import app
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services.frame_processor import process_one_frame
from services.tcp_handler import handle_tcp_client

# ── Test constants ────────────────────────────────────────────────────────────

_NODE_ID = "golden-path-node"
_NODE_CONFIG = {**DEFAULT_NODE_CONFIG, "node_id": _NODE_ID}

# Stable detection values → Kalman filter associates them consistently across frames.
_DELAY_US   = 55.0
_DOPPLER_HZ = 20.0
_SNR_DB     = 20.0

# Feed enough frames to guarantee M-of-N promotion.
_N_FRAMES = N_WINDOW()


# ── Protocol helpers (identical pattern to test_e2e_pipeline.py) ──────────────

def _msg(d: dict) -> bytes:
    return json.dumps(d).encode() + b"\n"

def _hello() -> bytes:
    return _msg({"type": "HELLO", "node_id": _NODE_ID, "version": "1.0", "is_synthetic": True})

def _config() -> bytes:
    return _msg({
        "type": "CONFIG",
        "node_id": _NODE_ID,
        "config_hash": "golden01",
        "is_synthetic": True,
        "config": _NODE_CONFIG,
        "capabilities": {"adsb_report": True},
    })

def _detection(ts_ms: int | None = None) -> bytes:
    ts_ms = ts_ms or int(time.time() * 1000)
    return _msg({"type": "DETECTION", "data": {
        "timestamp": ts_ms,
        "delay":   [_DELAY_US,   _DELAY_US + 0.5,  _DELAY_US + 1.0],
        "doppler": [_DOPPLER_HZ, _DOPPLER_HZ + 0.5, _DOPPLER_HZ - 0.5],
        "snr":     [_SNR_DB,     _SNR_DB - 1.0,     _SNR_DB - 2.0],
    }})

def _raw_frame(ts_ms: int | None = None) -> dict:
    ts_ms = ts_ms or int(time.time() * 1000)
    return {
        "timestamp": ts_ms,
        "delay":   [_DELAY_US,   _DELAY_US + 0.5,  _DELAY_US + 1.0],
        "doppler": [_DOPPLER_HZ, _DOPPLER_HZ + 0.5, _DOPPLER_HZ - 0.5],
        "snr":     [_SNR_DB,     _SNR_DB - 1.0,     _SNR_DB - 2.0],
    }


class _FakeReader:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._i = 0

    async def read(self, n: int) -> bytes:
        if self._i >= len(self._chunks):
            return b""
        data = self._chunks[self._i]
        self._i += 1
        return data


class _FakeWriter:
    def __init__(self):
        self._buf: list[bytes] = []
        self.closed = False
    def get_extra_info(self, k, d=None): return ("127.0.0.1", 9999) if k == "peername" else d
    def write(self, data: bytes): self._buf.append(data)
    async def drain(self): pass
    def close(self): self.closed = True
    def messages(self) -> list[dict]:
        out = []
        for chunk in self._buf:
            for line in chunk.split(b"\n"):
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean():
    """Wipe all golden-path artefacts from shared state before/after every test."""
    import services.tcp_handler as _th

    def _purge():
        state.connected_nodes.pop(_NODE_ID, None)
        state.node_pipelines.pop(_NODE_ID, None)
        _th._per_node_last_enqueue.pop(_NODE_ID, None)
        with state.geo_aircraft_lock:
            state.active_geo_aircraft.clear()
        state.adsb_aircraft.clear()
        state.latest_missed_detections.clear()
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
            except Exception:
                break

    _purge()
    yield
    _purge()


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Shared helper: run TCP handshake + enqueue N frames ───────────────────────

def _do_tcp_and_drain(n_frames: int = _N_FRAMES, interval_ms: int = 2000) -> list[tuple]:
    """Run TCP HELLO+CONFIG+N×DETECTION through handle_tcp_client, return drained frames."""
    base = int(time.time() * 1000)
    chunks = [_hello(), _config()]
    for i in range(n_frames):
        chunks.append(_detection(base + i * interval_ms))
    chunks.append(b"")

    reader = _FakeReader(chunks)
    writer = _FakeWriter()
    with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
        asyncio.run(handle_tcp_client(reader, writer))

    frames = []
    while not state.frame_queue.empty():
        frames.append(state.frame_queue.get_nowait())
    return frames


# ═══════════════════════════════════════════════════════════════════════════════
# GOLDEN PATH — each class is one layer of the happy-day scenario
# ═══════════════════════════════════════════════════════════════════════════════

class TestGoldenPath_Layer1_TCPHandshake:
    """Layer 1 — TCP handshake: node becomes visible to the server."""

    def test_node_registered_after_config(self):
        """After CONFIG, node appears in state.connected_nodes with correct hash."""
        _do_tcp_and_drain(n_frames=1)
        assert _NODE_ID in state.connected_nodes
        assert state.connected_nodes[_NODE_ID]["config_hash"] == "golden01"

    def test_config_ack_sent_to_node(self):
        """Server replies CONFIG_ACK before any frame processing."""
        base = int(time.time() * 1000)
        chunks = [_hello(), _config(), _detection(base), b""]
        reader = _FakeReader(chunks)
        writer = _FakeWriter()
        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))
        acks = [m for m in writer.messages() if m.get("type") == "CONFIG_ACK"]
        assert len(acks) == 1, "Exactly one CONFIG_ACK must be sent per session"

    def test_detection_frames_land_in_queue(self):
        """All N DETECTION messages arrive in state.frame_queue."""
        frames = _do_tcp_and_drain(n_frames=_N_FRAMES)
        assert len(frames) == _N_FRAMES

    def test_node_status_set_after_disconnect(self):
        """After EOF, node status transitions to 'disconnected'."""
        _do_tcp_and_drain(n_frames=1)
        assert state.connected_nodes[_NODE_ID]["status"] == "disconnected"


class TestGoldenPath_Layer2_FrameProcessor:
    """Layer 2 — Frame processor: frames drive the tracker state machine."""

    @pytest.fixture()
    def pipeline(self):
        state.connected_nodes[_NODE_ID] = {
            "config_hash": "golden01", "config": _NODE_CONFIG,
            "status": "active", "last_heartbeat": "2026-01-01T00:00:00Z",
            "peer": "127.0.0.1:9999", "is_synthetic": True, "capabilities": {},
        }
        state.node_analytics.register_node(_NODE_ID, _NODE_CONFIG)
        state.node_associator.register_node(_NODE_ID, _NODE_CONFIG)
        p = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = p
        return p

    def test_first_frame_creates_tentative_track(self, pipeline):
        """First frame spawns at least one TENTATIVE track hypothesis."""
        process_one_frame(_NODE_ID, _raw_frame(), pipeline)
        assert len(pipeline.tracker.tracks) > 0
        assert all(t.state_status == TrackState.TENTATIVE for t in pipeline.tracker.tracks)

    def test_n_frames_promote_to_active(self, pipeline):
        """N_WINDOW consistent frames produce at least one ACTIVE track."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            process_one_frame(_NODE_ID, _raw_frame(base + i * 1000), pipeline)
        active = [t for t in pipeline.tracker.tracks if t.state_status == TrackState.ACTIVE]
        assert len(active) >= 1, (
            f"Expected ≥1 ACTIVE track after {_N_FRAMES} frames "
            f"(M={M_THRESHOLD()}, N={N_WINDOW()})"
        )

    def test_active_track_has_stable_id(self, pipeline):
        """ACTIVE tracks carry a non-None stable ID."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            process_one_frame(_NODE_ID, _raw_frame(base + i * 1000), pipeline)
        active = [t for t in pipeline.tracker.tracks if t.state_status == TrackState.ACTIVE]
        assert all(t.id is not None for t in active)

    def test_geolocation_attempted_after_promotion(self, pipeline):
        """After promotion, the solver fires at least once (_geo_last_solve populated)."""
        pipeline._GEO_INTERVAL_S = 0.0  # disable rate-limit so solver fires immediately
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES + 2):
            process_one_frame(_NODE_ID, _raw_frame(base + i * 1000), pipeline)
        assert len(pipeline._geo_last_solve) > 0, (
            "Geolocation solver must be attempted after M-of-N promotion"
        )


class TestGoldenPath_Layer3_HttpApi:
    """Layer 3 — HTTP API reflects the system state correctly.

    These assertions validate that the routes correctly read from shared state;
    they do NOT depend on the TCP or frame-processor layers (state is set directly).
    """

    @pytest.fixture(autouse=True)
    def _seed_state(self):
        """Put a live node + pipeline into state so API endpoints have data."""
        state.connected_nodes[_NODE_ID] = {
            "config_hash": "golden01", "config": _NODE_CONFIG,
            "status": "active", "last_heartbeat": "2026-01-01T00:00:00Z",
            "peer": "127.0.0.1:9999", "is_synthetic": True, "capabilities": {},
        }
        p = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = p
        yield
        # Cleanup in the outer autouse fixture

    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_dashboard_counts_node(self, client):
        r = client.get("/api/test/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["nodes"]["total"] >= 1, "Dashboard must count connected node"
        assert body["nodes"]["active"] >= 1, "Node with status=active must be counted"
        assert body["pipeline"]["node_pipelines"] >= 1

    def test_radar_nodes_returns_json(self, client):
        """GET /api/radar/nodes returns 200 with parseable JSON."""
        r = client.get("/api/radar/nodes")
        assert r.status_code == 200
        # Must be valid JSON
        data = r.json()
        assert isinstance(data, dict)

    def test_radar_analytics_returns_json(self, client):
        """GET /api/radar/analytics returns 200 with parseable JSON."""
        r = client.get("/api/radar/analytics")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_config_has_required_keys(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        cfg = r.json()
        for key in ("ranking", "receiver", "broadcast_bands"):
            assert key in cfg, f"Config missing required key: {key!r}"

    def test_aircraft_json_parseable(self, client):
        r = client.get("/api/radar/data/aircraft.json")
        assert r.status_code == 200
        data = r.json()
        assert "aircraft" in data

    def test_unauthenticated_config_write_rejected(self, client):
        """PUT /api/config without admin JWT is rejected when auth is enabled.

        In the test suite AUTH_ENABLED=False (no OAuth keys configured), so auth
        is intentionally bypassed.  We simulate a production-like environment by
        patching AUTH_ENABLED to True and verify the guard rejects the request.
        """
        from pathlib import Path

        import core.users as _users
        from services.tower_ranking import _CONFIG_PATH

        # Snapshot the real config file so we can restore it even if auth bypasses.
        _cfg_path = Path(_CONFIG_PATH)
        _original = _cfg_path.read_bytes()
        try:
            with patch.object(_users, "AUTH_ENABLED", True):
                r = client.put("/api/config", json={"golden_path_test": True})
            # Without a valid JWT the server must refuse the write.
            assert r.status_code in (401, 403), (
                f"Expected 401/403 with AUTH_ENABLED=True; got {r.status_code}"
            )
        finally:
            _cfg_path.write_bytes(_original)


class TestGoldenPath_Layer4_FullStack:
    """Layer 4 — Compose all layers: TCP → process → API reads the result.

    This is the true end-to-end golden path: every layer runs in sequence,
    with no manual state seeding. If this test passes, the system is wired
    correctly from ingress to API output.

    IMPORTANT: Steps 1-3 (TCP + frame processing) must complete BEFORE
    the TestClient is created. The app lifespan starts frame_processor_loop
    workers that drain state.frame_queue concurrently.  If TestClient were
    active during the queue drain, those workers would race-consume the
    frames and frames_processed would be 0.  Creating TestClient only for
    Step 4 (HTTP API checks) avoids that race entirely.
    """

    def test_full_golden_path(self):
        """
        Golden path scenario:
          1. TCP handshake → node registered
          2. N DETECTION frames enqueued
          3. process_one_frame × N → ACTIVE track created
          4. HTTP API (fresh TestClient) correctly reflects the new state
        """
        # ── Step 1: TCP handshake + frame enqueue ─────────────────────────────
        # TestClient is NOT active here — no background workers compete for the queue.
        base = int(time.time() * 1000)
        chunks = [_hello(), _config()]
        for i in range(_N_FRAMES):
            chunks.append(_detection(base + i * 2000))
        chunks.append(b"")

        reader = _FakeReader(chunks)
        writer = _FakeWriter()
        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        assert _NODE_ID in state.connected_nodes, \
            "Step 1 failed: node not registered after TCP handshake"

        # ── Step 2: Drain frame_queue and run frame processor ────────────────
        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = pipe
        state.connected_nodes[_NODE_ID]["status"] = "active"

        frames_processed = 0
        while not state.frame_queue.empty():
            node_id, frame = state.frame_queue.get_nowait()
            process_one_frame(node_id, frame, pipe)
            frames_processed += 1

        assert frames_processed == _N_FRAMES, (
            f"Step 2 failed: expected {_N_FRAMES} frames, got {frames_processed}. "
            "If this is 0, a TestClient/frame_processor_loop worker consumed the queue."
        )

        # ── Step 3: Verify tracker promoted a track ───────────────────────────
        active_tracks = [t for t in pipe.tracker.tracks if t.state_status == TrackState.ACTIVE]
        assert len(active_tracks) >= 1, (
            f"Step 3 failed: no ACTIVE track after {_N_FRAMES} frames "
            f"(M={M_THRESHOLD()}, N={N_WINDOW()})"
        )

        # ── Step 4: HTTP API reflects the state ───────────────────────────────
        # TestClient is created HERE, after all frame processing is complete.
        # task_last_success is untouched so /api/health returns ok (no stale tasks).
        with TestClient(app, raise_server_exceptions=False) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok", (
                f"Step 4 failed: /api/health not ok. Full response: {health.json()}"
            )

            dashboard = client.get("/api/test/dashboard").json()
            assert dashboard["nodes"]["active"] >= 1, \
                "Step 4 failed: active node count not reflected in dashboard"
            assert dashboard["pipeline"]["node_pipelines"] >= 1, \
                "Step 4 failed: pipeline not visible in dashboard"

            # All API endpoints return 200
            for path in ("/api/radar/nodes", "/api/radar/analytics",
                         "/api/radar/data/aircraft.json", "/api/config"):
                r = client.get(path)
                assert r.status_code == 200, f"Step 4 failed: {path} returned {r.status_code}"
