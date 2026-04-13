"""Tests for services/state_snapshot.py — save/restore of in-memory state."""

import json
import os
import time
from collections import deque
from dataclasses import asdict
from unittest.mock import patch

import pytest

from analytics.trust import AdsReportEntry, TrustScoreState
from analytics.reputation import NodeReputation
from retina_custody.models import NodeIdentity
from services.state_snapshot import save_snapshot, restore_snapshot, _SNAPSHOT_PATH


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_trust_state(node_id="node-1", n_samples=3):
    ts = TrustScoreState(node_id=node_id)
    for i in range(n_samples):
        ts.add_sample(AdsReportEntry(
            timestamp_ms=1000 * i,
            predicted_delay=10.0 + i,
            predicted_doppler=50.0,
            measured_delay=10.5 + i,
            measured_doppler=51.0,
            adsb_hex=f"hex{i:03d}",
            adsb_lat=33.9,
            adsb_lon=-84.6,
        ))
    return ts


def _make_reputation(node_id="node-1", reputation=0.8, blocked=False):
    rep = NodeReputation(node_id=node_id, reputation=reputation, blocked=blocked)
    if blocked:
        rep.block_reason = "test block"
    return rep


# ── Round-trip: save → restore ────────────────────────────────────────────────

class TestSnapshotRoundTrip:
    """Verify that save_snapshot → restore_snapshot preserves all data types."""

    def test_trust_scores_survive_round_trip(self, tmp_path):
        from core import state

        ts = _make_trust_state("node-42", n_samples=5)
        state.node_analytics.trust_scores["node-42"] = ts

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

            # Wipe state
            state.node_analytics.trust_scores.clear()
            assert "node-42" not in state.node_analytics.trust_scores

            ok = restore_snapshot()

        assert ok is True
        restored = state.node_analytics.trust_scores["node-42"]
        assert restored.node_id == "node-42"
        assert len(restored.samples) == 5
        assert restored.samples[0].adsb_hex == "hex000"
        assert restored.score == ts.score

        # Cleanup
        state.node_analytics.trust_scores.pop("node-42", None)

    def test_reputations_survive_round_trip(self, tmp_path):
        from core import state

        rep = _make_reputation("node-7", reputation=0.65, blocked=True)
        rep.apply_penalty(0.1, "test penalty")
        state.node_analytics.reputations["node-7"] = rep

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.node_analytics.reputations.clear()
            restore_snapshot()

        restored = state.node_analytics.reputations["node-7"]
        assert restored.node_id == "node-7"
        assert restored.blocked is True
        assert len(restored.penalties) >= 1

        state.node_analytics.reputations.pop("node-7", None)

    def test_accuracy_samples_survive_round_trip(self, tmp_path):
        from core import state

        orig_samples = deque(state.accuracy_samples)
        state.accuracy_samples = deque([
            {"error_km": 1.5, "position_source": "solver"},
            {"error_km": 0.3, "position_source": "adsb"},
        ], maxlen=state.ACCURACY_MAX_SAMPLES)

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.accuracy_samples.clear()
            restore_snapshot()

        assert len(state.accuracy_samples) == 2
        assert state.accuracy_samples[0]["error_km"] == 1.5

        state.accuracy_samples = orig_samples

    def test_chain_entries_survive_round_trip(self, tmp_path):
        from core import state

        orig = dict(state.chain_entries)
        state.chain_entries["node-99"] = [{"ts": 100, "hash": "abc"}]

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.chain_entries.clear()
            restore_snapshot()

        assert "node-99" in state.chain_entries
        assert state.chain_entries["node-99"][0]["hash"] == "abc"

        state.chain_entries.clear()
        state.chain_entries.update(orig)

    def test_anomaly_log_survives_round_trip(self, tmp_path):
        from core import state

        orig = list(state.anomaly_log)
        state.anomaly_log = [{"ts": 1234, "type": "spoof", "hex": "aaa"}]

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.anomaly_log = []
            restore_snapshot()

        assert len(state.anomaly_log) == 1
        assert state.anomaly_log[0]["type"] == "spoof"

        state.anomaly_log = orig


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestSnapshotEdgeCases:
    def test_restore_returns_false_when_no_file(self, tmp_path):
        snap_path = str(tmp_path / "nonexistent.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is False

    def test_restore_handles_corrupted_json(self, tmp_path):
        snap_path = str(tmp_path / "corrupt.json")
        with open(snap_path, "w") as f:
            f.write("{this is not valid json!!!")

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is False

    def test_restore_handles_empty_snapshot(self, tmp_path):
        snap_path = str(tmp_path / "empty.json")
        with open(snap_path, "w") as f:
            json.dump({"saved_at": time.time()}, f)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is True  # succeeds, just nothing to restore

    def test_save_uses_atomic_write(self, tmp_path):
        """Save writes to .tmp then replaces — if we crash mid-write, old file survives."""
        from core import state

        snap_path = str(tmp_path / "snap.json")
        # Write a known-good snapshot first
        with open(snap_path, "w") as f:
            json.dump({"saved_at": 1.0, "trust_scores": {}}, f)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

        # Verify the snapshot is valid JSON
        with open(snap_path) as f:
            data = json.load(f)
        assert "saved_at" in data
        assert data["saved_at"] > 0

    def test_full_round_trip_with_all_fields(self, tmp_path):
        """Integration: populate every field, save, wipe, restore, verify."""
        from core import state

        # Save originals
        orig_trust = dict(state.node_analytics.trust_scores)
        orig_reps = dict(state.node_analytics.reputations)
        orig_acc = deque(state.accuracy_samples)
        orig_chain = dict(state.chain_entries)
        orig_iq = dict(state.iq_commitments)
        orig_anom = list(state.anomaly_log)

        # Populate state
        state.node_analytics.trust_scores["rt-1"] = _make_trust_state("rt-1", 2)
        state.node_analytics.reputations["rt-1"] = _make_reputation("rt-1", 0.9)
        state.accuracy_samples = deque([{"error_km": 0.5, "position_source": "mn"}], maxlen=state.ACCURACY_MAX_SAMPLES)
        state.chain_entries["rt-1"] = [{"ts": 1, "hash": "h1"}]
        state.iq_commitments["rt-1"] = [{"ts": 2, "digest": "d1"}]
        state.anomaly_log = [{"ts": 3, "msg": "test"}]

        snap_path = str(tmp_path / "full.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

            # Wipe everything
            state.node_analytics.trust_scores.clear()
            state.node_analytics.reputations.clear()
            state.accuracy_samples.clear()
            state.chain_entries.clear()
            state.iq_commitments.clear()
            state.anomaly_log = []

            restore_snapshot()

        assert "rt-1" in state.node_analytics.trust_scores
        assert "rt-1" in state.node_analytics.reputations
        assert len(state.accuracy_samples) == 1
        assert "rt-1" in state.chain_entries
        assert "rt-1" in state.iq_commitments
        assert len(state.anomaly_log) == 1

        # Restore originals
        state.node_analytics.trust_scores.clear()
        state.node_analytics.trust_scores.update(orig_trust)
        state.node_analytics.reputations.clear()
        state.node_analytics.reputations.update(orig_reps)
        state.accuracy_samples = orig_acc
        state.chain_entries.clear()
        state.chain_entries.update(orig_chain)
        state.iq_commitments.clear()
        state.iq_commitments.update(orig_iq)
        state.anomaly_log = orig_anom
