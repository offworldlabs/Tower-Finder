"""Lightweight state snapshot: save/restore high-value in-memory state across restarts.

Saved every SAVE_INTERVAL_S (60 s) by a background task.  Restored once at startup.
Persists: trust_scores, reputations, accuracy_samples, chain_entries,
node_identities, iq_commitments, anomaly_log, simulation_config.
"""

import hashlib
import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict

from core import state
from services.alerting import send_alert

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
        "simulation_config": dict(state.simulation_config),
        # The SIM_FRAC_* baseline in force when this was written — restore
        # compares it against the running one to decide who wins.
        "simulation_env_baseline": dict(state._SIMULATION_ENV_BASELINE),
    }

    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    tmp = _SNAPSHOT_PATH + ".tmp"
    payload = json.dumps(snapshot)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    # The checksum travels INSIDE the file so payload and checksum change in
    # one atomic os.replace.  The old two-file scheme wrote the new checksum
    # before replacing the payload, so a crash in the gap made a *valid* old
    # snapshot fail its integrity check on boot — the server started empty,
    # losing trust scores, reputations, chain entries and node identities.
    # (Reordering the two writes only moves the window to the other side:
    # new payload + old checksum is rejected just the same.)
    envelope = json.dumps({"schema": 2, "sha256": checksum, "payload": payload})
    with open(tmp, "w") as f:
        f.write(envelope)
    os.replace(tmp, _SNAPSHOT_PATH)
    # Keep the side file current for anything that still looks at it, written
    # after the payload it describes; with the embedded checksum it is
    # informational only and restore no longer trusts it.
    sha_tmp = _SNAPSHOT_PATH + ".sha256.tmp"
    with open(sha_tmp, "w") as f:
        f.write(checksum)
    os.replace(sha_tmp, _SNAPSHOT_PATH + ".sha256")
    size = os.path.getsize(_SNAPSHOT_PATH)
    logging.info("State snapshot saved (%d bytes, sha256=%s)", size, checksum[:12])

    # Replicate to R2 for durability across container recreates
    from services.r2_client import is_enabled as r2_enabled
    from services.r2_client import upload_file

    if r2_enabled():
        if upload_file("snapshots/state_snapshot.json", _SNAPSHOT_PATH):
            logging.info("State snapshot replicated to R2")
        else:
            logging.warning("State snapshot R2 replication failed")
            send_alert(
                "r2_replication_failed",
                "State snapshot R2 replication failed — backup is stale",
                {},
            )


# Operator-settable keys carried across a restart.  Whitelisted rather than
# wholesale-updated so a key retired from the schema cannot be resurrected by
# an old snapshot.  `_updated_at` rides along deliberately — see below.
_SIM_CONFIG_RESTORE_KEYS = frozenset(
    {
        "frac_anomalous",
        "frac_drone",
        "frac_dark",
        "min_aircraft",
        "max_aircraft",
        "max_range_km",
        "n_nodes",
        "dual_fraction",
        "_updated_at",
    }
)


def _restore_simulation_config(snap: dict) -> None:
    """Restore the physics-tab config, unless the deploy changed the intent.

    Without this the dict is boot-state-only: a rebuild reverted whatever the
    operator had set in the Physics tab, and because `_updated_at` is stamped
    at import the fleet's poll loop then pushed those defaults into the
    *running* world within 5 s.  Persisting it makes an applied scene survive
    `docker compose up -d --build`.

    Precedence: a runtime PUT outranks the SIM_FRAC_* env baseline (that is
    the point of persisting it), but a deploy that *changes* SIM_FRAC_* is a
    deliberate change of intent and outranks a stale runtime tweak.  The two
    are separable because the baseline in force at write time travels in the
    snapshot.  A pre-env-seeding snapshot has no recorded baseline, so its
    effective baseline was the hardcoded fallbacks — compare against those.

    `_updated_at` is restored verbatim rather than re-stamped.  The fleet
    applies config only when the polled stamp strictly exceeds its last-seen
    one, so keeping the original value means a fleet that did NOT restart
    alongside the backend (it already holds these values) skips a pointless
    re-apply, while a fleet that DID restart still applies — its last-seen
    stamp resets to 0.0.  Re-stamping to now() would also make every backend
    restart look like an operator edit to the UI's drift detection.
    """
    sim_cfg = snap.get("simulation_config")
    if not isinstance(sim_cfg, dict):
        return

    running_baseline = dict(state._SIMULATION_ENV_BASELINE)
    saved_baseline = snap.get("simulation_env_baseline")
    if not isinstance(saved_baseline, dict):
        saved_baseline = dict(state._SIM_FRAC_FALLBACKS)

    if saved_baseline != running_baseline:
        logging.info(
            "SIM_FRAC_* changed since the snapshot was written (%s → %s) — "
            "keeping the deployed values, discarding the saved simulation config",
            saved_baseline,
            running_baseline,
        )
        return

    restored = {k: v for k, v in sim_cfg.items() if k in _SIM_CONFIG_RESTORE_KEYS}
    if not restored:
        return
    state.simulation_config.update(restored)
    logging.info("Simulation config restored from snapshot: %s", restored)


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
                raw = f.read()
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "payload" in parsed and "sha256" in parsed:
                # Schema 2: self-verifying envelope — checksum and payload are
                # atomic by construction.
                actual = hashlib.sha256(parsed["payload"].encode()).hexdigest()
                if actual != parsed["sha256"]:
                    logging.error(
                        "State snapshot checksum mismatch (expected=%s, got=%s) — skipping corrupt file",
                        str(parsed["sha256"])[:12],
                        actual[:12],
                    )
                    send_alert("snapshot_corrupt", "State snapshot checksum mismatch — starting with empty state")
                else:
                    snap = json.loads(parsed["payload"])
            else:
                # Legacy schema 1: bare payload with a .sha256 side file that
                # was written non-atomically — verify when present, but a
                # mismatch here may be the old crash window rather than
                # corruption, so log loudly and still refuse (the R2 fallback
                # below gets a chance).
                sha_path = _SNAPSHOT_PATH + ".sha256"
                if os.path.exists(sha_path):
                    with open(sha_path) as _sha_f:
                        expected = _sha_f.read().strip()
                    actual = hashlib.sha256(raw.encode()).hexdigest()
                    if actual != expected:
                        logging.error(
                            "State snapshot checksum mismatch (expected=%s, got=%s) — skipping corrupt file",
                            expected[:12],
                            actual[:12],
                        )
                        send_alert("snapshot_corrupt", "State snapshot checksum mismatch — starting with empty state")
                        parsed = None
                snap = parsed
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
                    # Schema-2 envelope replicated to R2: verify and unwrap.
                    if isinstance(snap, dict) and "payload" in snap and "sha256" in snap:
                        actual = hashlib.sha256(snap["payload"].encode()).hexdigest()
                        if actual != snap["sha256"]:
                            logging.error("R2 state snapshot checksum mismatch — ignoring")
                            snap = None
                        else:
                            snap = json.loads(snap["payload"])
                    if snap is not None:
                        logging.info("State snapshot loaded from R2")
                except Exception:
                    logging.exception("Failed to parse R2 state snapshot")
                    snap = None

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

    # Simulation physics config
    _restore_simulation_config(snap)

    logging.info(
        "State snapshot restored: %d trust scores, %d reputations, %d accuracy samples",
        len(snap.get("trust_scores", {})),
        len(snap.get("reputations", {})),
        len(samples_list),
    )
    return True
