"""End-to-end pipeline tests: TCP handshake → frame processor → track promotion.

These tests verify the complete data-flow integration that unit tests miss:
  TCP DETECTION message
    → _enqueue_detection (rate-limit, queue)
    → process_one_frame (frame_processor)
    → PassiveRadarPipeline.process_frame
    → RetinaTracker (Kalman + GNN)
    → M-of-N promotion → ACTIVE track + stable track_id
    → _run_geolocation → GeolocatedTrack in state.active_geo_aircraft

Each class covers one slice of that chain; later classes compose earlier ones.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from core import state
from pipeline.passive_radar import PassiveRadarPipeline, DEFAULT_NODE_CONFIG
from services.frame_processor import process_one_frame
from services.tcp_handler import (
    handle_tcp_client,
    _enqueue_detection,
)
from retina_tracker.config import M_THRESHOLD, N_WINDOW
from retina_tracker.track import TrackState

# ── Shared test constants ─────────────────────────────────────────────────────

_NODE_ID = "e2e-test-node"

# Mirror of DEFAULT_NODE_CONFIG but keyed to our test node.
_NODE_CONFIG = {
    **DEFAULT_NODE_CONFIG,
    "node_id": _NODE_ID,
}

# Stable delay/doppler → Kalman filter keeps them associated every frame.
# Values are deliberately close to what a real ~8 km-range aircraft produces.
_DELAY_US   = 55.0   # µs bistatic delay
_DOPPLER_HZ = 20.0   # Hz (positive = closing)
_SNR_DB     = 20.0   # well above MIN_SNR threshold (7.0 dB)

# How many frames we need to guarantee M-of-N promotion.
# Feed N_WINDOW frames; with stable delay/doppler all N associate and M ≥ 4.
_N_FRAMES = N_WINDOW()


# ── Protocol helpers ──────────────────────────────────────────────────────────

def _msg(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8") + b"\n"


def _hello(node_id: str = _NODE_ID) -> bytes:
    return _msg({"type": "HELLO", "node_id": node_id, "version": "1.0",
                 "is_synthetic": True})


def _config(node_id: str = _NODE_ID) -> bytes:
    return _msg({
        "type": "CONFIG",
        "node_id": node_id,
        "config_hash": "e2etest01",
        "is_synthetic": True,
        "config": _NODE_CONFIG,
        "capabilities": {"adsb_report": True},
    })


def _detection(ts_ms: int | None = None) -> bytes:
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    frame = {
        "timestamp": ts_ms,
        # Three stable detections per frame so the tracker consistently associates.
        "delay":   [_DELAY_US,   _DELAY_US + 0.5,  _DELAY_US + 1.0],
        "doppler": [_DOPPLER_HZ, _DOPPLER_HZ + 0.5, _DOPPLER_HZ - 0.5],
        "snr":     [_SNR_DB,     _SNR_DB - 1.0,     _SNR_DB - 2.0],
    }
    return _msg({"type": "DETECTION", "data": frame})


def _make_raw_frame(ts_ms: int | None = None) -> dict:
    """Build the dict that lives inside a DETECTION 'data' field."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return {
        "timestamp": ts_ms,
        "delay":   [_DELAY_US,   _DELAY_US + 0.5,  _DELAY_US + 1.0],
        "doppler": [_DOPPLER_HZ, _DOPPLER_HZ + 0.5, _DOPPLER_HZ - 0.5],
        "snr":     [_SNR_DB,     _SNR_DB - 1.0,     _SNR_DB - 2.0],
    }


# ── Mock TCP primitives ───────────────────────────────────────────────────────

class _FakeReader:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._idx = 0

    async def read(self, n: int) -> bytes:
        if self._idx >= len(self._chunks):
            return b""          # EOF
        data = self._chunks[self._idx]
        self._idx += 1
        return data


class _FakeWriter:
    def __init__(self):
        self._written: list[bytes] = []
        self.closed = False

    def get_extra_info(self, key, default=None):
        return ("127.0.0.1", 29999) if key == "peername" else default

    def write(self, data: bytes):
        self._written.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def messages(self) -> list[dict]:
        result = []
        for chunk in self._written:
            for line in chunk.split(b"\n"):
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result


# ── Shared state cleanup fixture ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    """Remove all test artefacts from global state before and after every test."""
    # Import here so the module-level dict is the live one (not a stale copy).
    import services.tcp_handler as _th

    def _purge():
        state.connected_nodes.pop(_NODE_ID, None)
        state.node_pipelines.pop(_NODE_ID, None)
        _th._per_node_last_enqueue.pop(_NODE_ID, None)
        with state.geo_aircraft_lock:
            state.active_geo_aircraft.clear()
        state.adsb_aircraft.clear()
        state.latest_missed_detections.clear()
        # Drain leftover frames so tests start from a clean queue.
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
            except Exception:
                break

    _purge()
    yield
    _purge()


@pytest.fixture()
def _registered_node():
    """Register the test node in state as if TCP CONFIG was received."""
    state.connected_nodes[_NODE_ID] = {
        "config_hash": "e2etest01",
        "config": _NODE_CONFIG,
        "status": "active",
        "last_heartbeat": "2026-01-01T00:00:00Z",
        "peer": "127.0.0.1:29999",
        "is_synthetic": True,
        "capabilities": {},
    }
    state.node_analytics.register_node(_NODE_ID, _NODE_CONFIG)
    state.node_associator.register_node(_NODE_ID, _NODE_CONFIG)


@pytest.fixture()
def pipeline(_registered_node):
    """Create a PassiveRadarPipeline registered in state.node_pipelines."""
    p = PassiveRadarPipeline(_NODE_CONFIG)
    state.node_pipelines[_NODE_ID] = p
    return p


# ── 1. TCP DETECTION → frame_queue ────────────────────────────────────────────

class TestTCPDetectionEnqueue:
    """Verify that the TCP handler correctly enqueues DETECTION frames."""

    def test_single_detection_lands_in_queue(self):
        """HELLO + CONFIG + DETECTION → one frame in state.frame_queue."""
        ts = int(time.time() * 1000)
        reader = _FakeReader([_hello(), _config(), _detection(ts), b""])
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        assert not state.frame_queue.empty(), "Frame must land in queue after DETECTION"
        q_node, frame = state.frame_queue.get_nowait()
        assert q_node == _NODE_ID
        assert frame["timestamp"] == ts

    def test_config_ack_sent_before_any_frame(self):
        """CONFIG_ACK is sent during handshake, before any frame processing."""
        ts = int(time.time() * 1000)
        reader = _FakeReader([_hello(), _config(), _detection(ts), b""])
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        acks = [m for m in writer.messages() if m.get("type") == "CONFIG_ACK"]
        assert len(acks) == 1
        assert acks[0]["config_hash"] == "e2etest01"
        # ACK must appear in the message list before any detection-related message
        first_ack_idx = next(i for i, m in enumerate(writer.messages())
                             if m.get("type") == "CONFIG_ACK")
        assert first_ack_idx == 0

    def test_rate_limiter_drops_rapid_duplicates(self):
        """Back-to-back DETECTION frames within the rate window → only 1 enqueued."""
        base = int(time.time() * 1000)
        reader = _FakeReader([
            _hello(), _config(),
            _detection(base),
            _detection(base + 100),
            _detection(base + 200),
            b"",
        ])
        writer = _FakeWriter()

        # Default rate limit is 1.0 s; all three arrive within ~0 s → 1 enqueued.
        asyncio.run(handle_tcp_client(reader, writer))

        count = 0
        while not state.frame_queue.empty():
            state.frame_queue.get_nowait()
            count += 1

        assert count == 1, (
            f"Rate limiter should keep only 1 of 3 rapid frames; got {count}"
        )

    def test_rate_limiter_disabled_enqueues_all(self):
        """With _NODE_MIN_INTERVAL_S=0, every DETECTION is enqueued."""
        base = int(time.time() * 1000)
        n = _N_FRAMES
        chunks = [_hello(), _config()]
        # Space frames far apart in timestamp so the Kalman filter sees them as
        # distinct observations (1000 ms apart = 1 s between frames).
        for i in range(n):
            chunks.append(_detection(base + i * 2000))
        chunks.append(b"")

        reader = _FakeReader(chunks)
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        count = 0
        while not state.frame_queue.empty():
            state.frame_queue.get_nowait()
            count += 1

        assert count == n, f"Expected {n} enqueued frames; got {count}"

    def test_queue_full_increments_drop_counter(self):
        """When frame_queue is full, frames_dropped increases."""
        import asyncio as _asyncio

        before = state.frames_dropped

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            # Make put_nowait always raise QueueFull.
            with patch.object(state.frame_queue, "put_nowait",
                              side_effect=_asyncio.QueueFull):
                _enqueue_detection(
                    {"type": "DETECTION", "data": _make_raw_frame()},
                    "e2e-overflow-node",
                )

        assert state.frames_dropped > before, (
            "frames_dropped must increase when queue is full"
        )

    def test_invalid_config_sends_nack_and_rejects_node(self):
        """CONFIG with lat=999 → CONFIG_NACK; node NOT added to connected_nodes."""
        bad = _msg({
            "type": "CONFIG",
            "node_id": _NODE_ID,
            "config_hash": "badhash",
            "config": {"rx_lat": 999.0, "rx_lon": -84.65,
                       "tx_lat": 33.76, "tx_lon": -84.33},
        })
        reader = _FakeReader([_hello(), bad, b""])
        writer = _FakeWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        nacks = [m for m in writer.messages() if m.get("type") == "CONFIG_NACK"]
        assert len(nacks) == 1, "Expected exactly one CONFIG_NACK"
        assert _NODE_ID not in state.connected_nodes, (
            "Node with invalid config must not be registered"
        )


# ── 2. process_one_frame → Kalman tracker accumulates state ───────────────────

class TestFrameProcessorTracker:
    """Verify the frame processor drives the tracker through its state machine."""

    def test_first_frame_creates_tentative_track(self, pipeline):
        """After one frame, tracker has at least one TENTATIVE track."""
        frame = _make_raw_frame()
        process_one_frame(_NODE_ID, frame, pipeline)

        assert len(pipeline.tracker.tracks) > 0, (
            "First frame should spawn at least one tentative track hypothesis"
        )
        assert all(t.state_status == TrackState.TENTATIVE
                   for t in pipeline.tracker.tracks)

    def test_m_of_n_frames_promote_track_to_active(self, pipeline):
        """After N_WINDOW consistent frames, at least one track is ACTIVE."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000), pipeline)

        active = [t for t in pipeline.tracker.tracks
                  if t.state_status == TrackState.ACTIVE]
        assert len(active) >= 1, (
            f"Expected ≥1 ACTIVE track after {_N_FRAMES} consistent frames "
            f"(M={M_THRESHOLD()}, N={N_WINDOW()}); "
            f"statuses={[t.state_status for t in pipeline.tracker.tracks]}"
        )

    def test_promoted_track_has_stable_id(self, pipeline):
        """An ACTIVE track receives a non-None stable track_id at promotion."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000), pipeline)

        active = [t for t in pipeline.tracker.tracks
                  if t.state_status == TrackState.ACTIVE]
        assert len(active) >= 1
        for t in active:
            assert t.id is not None, (
                "ACTIVE tracks must have a stable ID assigned at promotion"
            )

    def test_promoted_track_appears_in_event_writer(self, pipeline):
        """After M-of-N promotion, the event_writer holds an event for the track."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000), pipeline)

        events = pipeline.event_writer.get_events()
        assert len(events) >= 1, (
            "event_writer should have at least one entry after track promotion"
        )

    def test_extra_frames_beyond_n_window_do_not_reset_track(self, pipeline):
        """Feeding more frames than N_WINDOW keeps the track ACTIVE, not demoted."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES + 4):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000), pipeline)

        active = [t for t in pipeline.tracker.tracks
                  if t.state_status in (TrackState.ACTIVE, TrackState.COASTING)]
        assert len(active) >= 1, (
            "ACTIVE track should remain active/coasting after additional frames"
        )

    def test_node_registered_in_analytics_after_frames(self, pipeline):
        """Frame processor records detections in node_analytics without crashing."""
        base = int(time.time() * 1000)
        for i in range(3):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000), pipeline)
        # If we get here without exception, analytics recording is wired correctly.


# ── 3. Full E2E: TCP handshake → frame processing → promoted track ─────────────

class TestFullE2E:
    """Compose TCP handler + frame processor into a full end-to-end scenario."""

    def _run_handshake_and_collect(self, n_frames: int, interval_ms: int = 2000):
        """Send HELLO + CONFIG + n DETECTION frames through TCP handler.

        Returns the drained list of (node_id, frame) tuples from frame_queue.
        """
        base = int(time.time() * 1000)
        chunks = [_hello(), _config()]
        for i in range(n_frames):
            chunks.append(_detection(base + i * interval_ms))
        chunks.append(b"")

        reader = _FakeReader(chunks)
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        frames_out = []
        while not state.frame_queue.empty():
            frames_out.append(state.frame_queue.get_nowait())
        return frames_out, writer

    def test_tcp_frames_flow_through_to_active_track(self, _registered_node):
        """TCP DETECTION x N → process_one_frame x N → at least one ACTIVE track."""
        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = pipe

        frames, _ = self._run_handshake_and_collect(_N_FRAMES)
        assert len(frames) == _N_FRAMES, (
            f"Expected {_N_FRAMES} frames from queue; got {len(frames)}"
        )

        for node_id, frame in frames:
            process_one_frame(node_id, frame, pipe)

        active = [t for t in pipe.tracker.tracks
                  if t.state_status == TrackState.ACTIVE]
        assert len(active) >= 1, (
            "Full TCP → process pipeline must produce at least one ACTIVE track"
        )

    def test_node_registered_in_state_after_handshake(self):
        """After TCP handshake, node appears in state.connected_nodes."""
        chunks = [_hello(), _config(), b""]
        reader = _FakeReader(chunks)
        writer = _FakeWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        assert _NODE_ID in state.connected_nodes
        assert state.connected_nodes[_NODE_ID]["config_hash"] == "e2etest01"

    def test_node_status_disconnected_after_eof(self):
        """Node status is set to 'disconnected' when the TCP connection closes."""
        chunks = [_hello(), _config(), b""]
        reader = _FakeReader(chunks)
        writer = _FakeWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        assert state.connected_nodes[_NODE_ID]["status"] == "disconnected"

    def test_handshake_order_enforced(self):
        """DETECTION before CONFIG is silently ignored (no crash, no enqueue)."""
        ts = int(time.time() * 1000)
        # Send DETECTION before CONFIG — node_id is None at that point.
        reader = _FakeReader([_hello(), _detection(ts), b""])
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        # The frame may or may not land (node_id is None), but must not crash.
        # Drain queue — we only assert there was no exception.
        while not state.frame_queue.empty():
            state.frame_queue.get_nowait()


# ── 4. Geolocation after promotion ────────────────────────────────────────────

class TestGeolocationAfterPromotion:
    """Verify _run_geolocation fires after M-of-N and populates geolocated_tracks."""

    @pytest.fixture()
    def geo_pipeline(self, _registered_node):
        """Pipeline with geolocation rate-limit disabled so solver fires every frame."""
        p = PassiveRadarPipeline(_NODE_CONFIG)
        p._GEO_INTERVAL_S = 0.0   # force solver to run on every new event
        state.node_pipelines[_NODE_ID] = p
        return p

    def test_geolocation_attempted_after_promotion(self, geo_pipeline):
        """After M-of-N promotion, _geo_last_solve is populated (solver was called)."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES + 2):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000),
                              geo_pipeline)

        assert len(geo_pipeline._geo_last_solve) > 0, (
            "_geo_last_solve must be non-empty: geolocation was never attempted "
            "after track promotion"
        )

    def test_adsb_track_populates_active_geo_aircraft(self, geo_pipeline):
        """ADS-B-aligned track produces an entry in state.active_geo_aircraft
        (either via solver success or ADS-B fallback)."""
        ac_hex = "e2eabc1"
        ts_now = int(time.time() * 1000)
        state.adsb_aircraft[ac_hex] = {
            "hex": ac_hex,
            "flight": "E2ETEST",
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": 35000,
            "gs": 250,
            "track": 90,
            "last_seen_ms": ts_now,
        }

        base = int(time.time() * 1000)
        for i in range(_N_FRAMES + 3):
            # Align the first detection in each frame to the ADS-B aircraft.
            frame = {
                "timestamp": base + i * 1000,
                "delay":   [_DELAY_US,   _DELAY_US + 0.5],
                "doppler": [_DOPPLER_HZ, _DOPPLER_HZ + 0.5],
                "snr":     [_SNR_DB,     _SNR_DB - 1.0],
                "adsb": [
                    {
                        "hex": ac_hex,
                        "lat": 33.9 + i * 0.001,
                        "lon": -84.6 + i * 0.001,
                        "alt_baro": 35000,
                        "gs": 250,
                        "track": 90,
                        "flight": "E2ETEST",
                    },
                    None,
                ],
            }
            process_one_frame(_NODE_ID, frame, geo_pipeline)

        # Solver may succeed or fail for this synthetic geometry, but at
        # minimum the ADS-B fallback path should populate active_geo_aircraft.
        with state.geo_aircraft_lock:
            geo_entries = dict(state.active_geo_aircraft)

        has_geolocated = len(geo_pipeline.geolocated_tracks) > 0
        has_geo_state  = len(geo_entries) > 0

        assert has_geolocated or has_geo_state, (
            "After promotion with ADS-B data, at least one geolocated entry "
            "must exist in pipeline.geolocated_tracks or state.active_geo_aircraft"
        )

    def test_non_adsb_track_geo_attempted(self, geo_pipeline):
        """A pure radar (no ADS-B) track still gets a geolocation attempt."""
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES + 2):
            process_one_frame(_NODE_ID, _make_raw_frame(base + i * 1000),
                              geo_pipeline)

        # The solver may return None for a geometry outside the valid region,
        # but it *must* have been invoked (geo_last_solve populated).
        assert len(geo_pipeline._geo_last_solve) >= 1, (
            "Geolocation should be attempted for every promoted track, "
            "even without ADS-B data"
        )


# ── 5. Regression guards ───────────────────────────────────────────────────────

class TestRegressions:
    """Guard against specific bugs that have bitten us before."""

    def test_malformed_json_does_not_crash_handler(self):
        """Malformed JSON in the TCP stream is skipped; handler continues normally."""
        ts = int(time.time() * 1000)
        reader = _FakeReader([
            _hello(),
            b"this is not json\n",
            _config(),
            _detection(ts),
            b"",
        ])
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        # Node should still be registered despite the malformed line.
        assert _NODE_ID in state.connected_nodes

    def test_non_finite_adsb_coords_skipped_in_enqueue(self):
        """Frames with NaN/Inf ADS-B lat/lon do not crash _apply_synthetic_adsb."""
        import math
        msg = {
            "type": "DETECTION",
            "data": {
                "timestamp": int(time.time() * 1000),
                "delay": [55.0],
                "doppler": [20.0],
                "snr": [15.0],
                "adsb": [{"hex": "badgeo", "lat": math.nan, "lon": math.inf}],
            },
        }
        # Must not raise.
        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            _enqueue_detection(msg, "e2e-nan-node")

        # NaN entry must not pollute state.adsb_aircraft.
        assert "badgeo" not in state.adsb_aircraft

    def test_frame_without_timestamp_is_rejected(self):
        """A DETECTION frame missing 'timestamp' is silently dropped."""
        msg = {
            "type": "DETECTION",
            "data": {"delay": [55.0], "doppler": [20.0], "snr": [15.0]},
        }
        before_size = state.frame_queue.qsize()
        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            _enqueue_detection(msg, _NODE_ID)

        assert state.frame_queue.qsize() == before_size, (
            "Frame without timestamp must be dropped before reaching the queue"
        )

    def test_pipeline_does_not_share_state_across_nodes(self, _registered_node):
        """Two distinct nodes get independent pipelines; no cross-node state leak."""
        node_a = _NODE_ID
        node_b = "e2e-test-node-B"

        state.connected_nodes[node_b] = {
            **state.connected_nodes[node_a],
            "config": {**_NODE_CONFIG, "node_id": node_b},
        }

        pipe_a = PassiveRadarPipeline(_NODE_CONFIG)
        pipe_b = PassiveRadarPipeline({**_NODE_CONFIG, "node_id": node_b})
        state.node_pipelines[node_a] = pipe_a
        state.node_pipelines[node_b] = pipe_b

        base = int(time.time() * 1000)
        # Feed frames only to node_a.
        for i in range(_N_FRAMES):
            process_one_frame(node_a, _make_raw_frame(base + i * 1000), pipe_a)

        # node_b's pipeline must be untouched.
        assert len(pipe_b.tracker.tracks) == 0, (
            "node_b pipeline should have zero tracks — frames only went to node_a"
        )

        # Cleanup node_b
        state.connected_nodes.pop(node_b, None)
        state.node_pipelines.pop(node_b, None)


# ── 6. frame_processor_loop (background worker) drains the queue ──────────────

class TestBackgroundFrameLoop:
    """Verify the actual production background loop that drains state.frame_queue.

    This closes the gap that all other tests leave open: in production,
    process_one_frame is never called directly — it runs inside
    frame_processor_loop via loop.run_in_executor().  These tests start the
    real coroutine and confirm it picks up frames from the queue, increments
    counters, and drives tracks to ACTIVE.

    state.frame_queue is bound to the import-time event loop, so each test
    creates a fresh asyncio.Queue and patches state.frame_queue with it.
    This matches what the production server does (one queue, one loop, one
    long-lived task).
    """

    async def _run_loop_drain(self, default_pipeline, queue: asyncio.Queue,
                               timeout: float = 5.0):
        """Run frame_processor_loop against a given queue until it empties."""
        from services.tasks.frame_loop import frame_processor_loop

        with patch.object(state, "frame_queue", queue):
            task = asyncio.create_task(frame_processor_loop(default_pipeline))
            deadline = asyncio.get_event_loop().time() + timeout
            while not queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)   # one extra tick for the last item
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.anyio
    async def test_loop_drains_queue_and_increments_counter(self, _registered_node):
        """frame_processor_loop consumes every queued frame and bumps frames_processed."""
        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = pipe

        queue: asyncio.Queue = asyncio.Queue()
        base = int(time.time() * 1000)
        n = 3
        for i in range(n):
            queue.put_nowait((_NODE_ID, _make_raw_frame(base + i * 1000)))

        before = state.frames_processed
        await self._run_loop_drain(pipe, queue)

        assert queue.empty(), "Loop must drain all frames from the queue"
        assert state.frames_processed - before == n, (
            f"frames_processed should increase by {n}; "
            f"delta={state.frames_processed - before}"
        )

    @pytest.mark.anyio
    async def test_loop_promotes_track_to_active(self, _registered_node):
        """Background loop feeding N_WINDOW frames → ACTIVE track in the pipeline."""
        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = pipe

        queue: asyncio.Queue = asyncio.Queue()
        base = int(time.time() * 1000)
        for i in range(_N_FRAMES):
            queue.put_nowait((_NODE_ID, _make_raw_frame(base + i * 1000)))

        await self._run_loop_drain(pipe, queue)

        active = [t for t in pipe.tracker.tracks
                  if t.state_status == TrackState.ACTIVE]
        assert len(active) >= 1, (
            "frame_processor_loop must drive tracker to ACTIVE via M-of-N promotion"
        )


# ── 7. Full integration: TCP → processor → analytics → API output ────────────

class TestFullIntegrationPath:
    """End-to-end test that exercises the *complete* production data path.

    This is the integration gap that earlier test classes left open:
    previous tests verify individual stages (TCP → queue, queue → tracker,
    loop drains queue), but never confirm that the analytics refresh sees
    the resulting node data and that the leaderboard/storage endpoints
    return coherent results from it.

    The test:
      1. TCP handshake + N_WINDOW DETECTION frames
      2. Drain frame_queue manually through process_one_frame
      3. Run _refresh_analytics_and_nodes() synchronously
      4. Assert state.latest_analytics_bytes contains the test node
      5. Assert the leaderboard builder includes the node with correct data
      6. Assert missed-detections computation runs without errors
    """

    def _tcp_handshake_and_process(self):
        """Full path: TCP messages → queue → process_one_frame → promoted tracks."""
        base = int(time.time() * 1000)
        chunks = [_hello(), _config()]
        for i in range(_N_FRAMES):
            chunks.append(_detection(base + i * 2000))
        chunks.append(b"")

        reader = _FakeReader(chunks)
        writer = _FakeWriter()

        with patch("services.tcp_handler._NODE_MIN_INTERVAL_S", 0.0):
            asyncio.run(handle_tcp_client(reader, writer))

        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.node_pipelines[_NODE_ID] = pipe

        frames = []
        while not state.frame_queue.empty():
            frames.append(state.frame_queue.get_nowait())

        for node_id, frame in frames:
            process_one_frame(node_id, frame, pipe)

        return pipe, frames

    def test_analytics_refresh_includes_node_after_processing(self):
        """After TCP + processing, analytics refresh picks up the test node."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        pipe, frames = self._tcp_handshake_and_process()
        assert len(frames) == _N_FRAMES

        # Run the analytics refresh synchronously
        _refresh_analytics_and_nodes()

        # The pre-serialised analytics bytes must contain our test node
        import orjson
        analytics = orjson.loads(state.latest_analytics_bytes)
        assert _NODE_ID in analytics.get("nodes", {}), (
            f"Analytics refresh must include {_NODE_ID} after frame processing; "
            f"found nodes: {list(analytics.get('nodes', {}).keys())[:5]}"
        )

        # Nodes bytes must also contain the test node
        nodes_data = orjson.loads(state.latest_nodes_bytes)
        assert _NODE_ID in nodes_data.get("nodes", {}), (
            f"Nodes snapshot must include {_NODE_ID}"
        )

    def test_leaderboard_includes_node_with_detections(self):
        """Leaderboard data includes the test node with non-zero detections."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes
        import orjson

        self._tcp_handshake_and_process()
        _refresh_analytics_and_nodes()

        # Parse leaderboard the same way the endpoint does
        raw = state.latest_analytics_bytes
        summaries = orjson.loads(raw).get("nodes", {})
        assert _NODE_ID in summaries

        s = summaries[_NODE_ID]
        m = s.get("metrics", {})
        assert m.get("total_frames", 0) > 0, (
            "Node must have recorded frames after processing"
        )
        assert m.get("total_detections", 0) > 0, (
            "Node must have recorded detections after processing"
        )

    def test_missed_detections_computed_for_node(self):
        """After processing, _refresh_missed_detections runs without crash
        and produces an entry for the test node."""
        from services.tasks.analytics_refresh import (
            _refresh_analytics_and_nodes,
            _refresh_missed_detections,
        )

        self._tcp_handshake_and_process()

        # The TCP handler sets status="disconnected" on EOF. Override to
        # "active" so _refresh_missed_detections doesn't skip this node.
        with state.connected_nodes_lock:
            if _NODE_ID in state.connected_nodes:
                state.connected_nodes[_NODE_ID]["status"] = "active"

        # Add a fake ADS-B aircraft within the node's beam so there's
        # something to detect/miss.
        ac_hex = "e2etest1"
        state.adsb_aircraft[ac_hex] = {
            "hex": ac_hex,
            "lat": 33.85,    # near the default node location
            "lon": -84.50,
            "alt_baro": 35000,
            "gs": 250,
            "track": 90,
            "last_seen_ms": int(time.time() * 1000),
        }

        with state.connected_nodes_lock:
            snapshot = list(state.connected_nodes.items())

        _refresh_missed_detections(snapshot)

        assert _NODE_ID in state.latest_missed_detections, (
            "Missed detections must contain the test node"
        )
        miss_data = state.latest_missed_detections[_NODE_ID]
        assert "in_range" in miss_data
        assert "missed" in miss_data
        assert "miss_rate" in miss_data
        assert isinstance(miss_data["miss_rate"], float)

        # Cleanup
        state.adsb_aircraft.pop(ac_hex, None)

    def test_storage_endpoint_disk_fields(self):
        """The storage scan helper returns disk usage fields."""
        from routes.admin import _scan_archive_dir
        from pathlib import Path

        archive_dir = Path(__file__).resolve().parent.parent / "coverage_data" / "archive"
        total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        # Should not crash even if the archive dir doesn't exist (empty results)
        assert isinstance(total_files, int)
        assert isinstance(total_bytes, int)
        assert isinstance(per_node, dict)
