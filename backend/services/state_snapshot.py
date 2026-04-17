"""Lightweight state snapshot: save/restore high-value in-memory state across restarts.

Saved every 5 minutes by a background task.  Restored once at startup.
Persists: trust_scores, reputations, accuracy_samples, chain_entries,
node_identities, iq_commitments, anomaly_log.
"""

import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict

from core import state

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_SNAPSHOT_PATH = os.path.join(_SNAPSHOT_DIR, "state_snapshot.json")
SAVE_INTERVAL_S = 60  # 1 minute


def save_snapshot() -> None:
    """Serialise high-value state to disk as JSON."""

    trust = {}
    for nid, ts in state.node_analytics.trust_scores.items():
        trust[nid] = {
            "node_id": ts.node_id,
            "samples": [asdict(s) for s in ts.samples],
            "max_samples": ts.max_samples,
            "delay_threshold_us": ts.delay_threshold_us,
            "doppler_threshold_hz": ts.doppler_threshold_hz,
        }

    reps = {}
    for nid, rep in state.node_analytics.reputations.items():
        reps[nid] = asdict(rep)

    identities = {}
    for nid, ident in state.node_identities.items():
        identities[nid] = ident.to_dict()

    snapshot = {
        "saved_at": time.time(),
        "trust_scores": trust,
        "reputations": reps,
        "accuracy_samples": list(state.accuracy_samples),
        "chain_entries": dict(state.chain_entries),
        "node_identities": identities,
        "iq_commitments": dict(state.iq_commitments),
        "anomaly_log": list(state.anomaly_log),
    }

    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    tmp = _SNAPSHOT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f)
    os.replace(tmp, _SNAPSHOT_PATH)
    size = os.path.getsize(_SNAPSHOT_PATH)
    logging.info("State snapshot saved (%d bytes)", size)

    # Replicate to R2 for durability across container recreates
    from services.r2_client import is_enabled as r2_enabled
    from services.r2_client import upload_file
    if r2_enabled():
        if upload_file("snapshots/state_snapshot.json", _SNAPSHOT_PATH):
            logging.info("State snapshot replicated to R2")
        else:
            logging.warning("State snapshot R2 replication failed")


def restore_snapshot() -> bool:
    """Load state from disk snapshot. Returns True if restored, False if no snapshot found."""
    from retina_analytics.reputation import NodeReputation
    from retina_analytics.trust import AdsReportEntry, TrustScoreState
    from retina_custody.models import NodeIdentity

    snap = None

    # Try local snapshot first
    if os.path.exists(_SNAPSHOT_PATH):
        try:
            with open(_SNAPSHOT_PATH) as f:
                snap = json.load(f)
        except Exception:
            logging.exception("Failed to read local state snapshot")

    # Fall back to R2 if local snapshot is missing or corrupt
    if snap is None:
        from services.r2_client import download_bytes
        from services.r2_client import is_enabled as r2_enabled
        if r2_enabled():
            logging.info("Trying R2 for state snapshot...")
            data = download_bytes("snapshots/state_snapshot.json")
            if data:
                try:
                    snap = json.loads(data)
                    logging.info("State snapshot loaded from R2")
                except Exception:
                    logging.exception("Failed to parse R2 state snapshot")

    if snap is None:
        logging.info("No state snapshot found (checked local + R2)")
        return False

    saved_at = snap.get("saved_at", 0)
    age_h = (time.time() - saved_at) / 3600
    logging.info("Restoring state snapshot (%.1f hours old)", age_h)

    # Trust scores
    for nid, ts_data in snap.get("trust_scores", {}).items():
        samples = [AdsReportEntry(**s) for s in ts_data.get("samples", [])]
        state.node_analytics.trust_scores[nid] = TrustScoreState(
            node_id=ts_data["node_id"],
            samples=samples,
            max_samples=ts_data.get("max_samples", 500),
            delay_threshold_us=ts_data.get("delay_threshold_us", 5.0),
            doppler_threshold_hz=ts_data.get("doppler_threshold_hz", 20.0),
        )

    # Reputations
    for nid, rep_data in snap.get("reputations", {}).items():
        state.node_analytics.reputations[nid] = NodeReputation(**rep_data)

    # Accuracy samples
    samples_list = snap.get("accuracy_samples", [])
    state.accuracy_samples = deque(samples_list, maxlen=state.ACCURACY_MAX_SAMPLES)

    # Chain entries
    state.chain_entries.update(snap.get("chain_entries", {}))

    # Node identities
    for nid, ident_data in snap.get("node_identities", {}).items():
        state.node_identities[nid] = NodeIdentity.from_dict(ident_data)

    # IQ commitments
    state.iq_commitments.update(snap.get("iq_commitments", {}))

    # Anomaly log
    state.anomaly_log = snap.get("anomaly_log", [])

    logging.info("State snapshot restored: %d trust scores, %d reputations, %d accuracy samples",
                 len(snap.get("trust_scores", {})),
                 len(snap.get("reputations", {})),
                 len(samples_list))
    return True
