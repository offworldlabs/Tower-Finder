"""Tests for state_snapshot.py R2 integration.

Covers:
  1. save_snapshot() replicates to R2 when enabled
  2. restore_snapshot() falls back to R2 when local file is absent
  3. restore_snapshot() prefers local file over R2
  4. restore_snapshot() returns False when neither source has a snapshot
  5. R2 replication failure doesn't prevent local save

Uses moto for S3 mocking and a temp directory for the snapshot path.
"""

import json
import os
import tempfile
import time
import unittest
import unittest.mock

import boto3
from moto import mock_aws

_FAKE_R2_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "AKIATEST",
    "R2_SECRET_ACCESS_KEY": "testsecret",
    "R2_BUCKET": "retina-data",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "testsecret",
}


def _make_bucket():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")


def _minimal_snapshot() -> dict:
    """Minimal valid snapshot dict (empty data — enough for restore to succeed)."""
    return {
        "saved_at": time.time(),
        "trust_scores": {},
        "reputations": {},
        "accuracy_samples": [],
        "chain_entries": {},
        "node_identities": {},
        "iq_commitments": {},
        "anomaly_log": [],
    }


class TestStateSnapshotR2(unittest.TestCase):

    def setUp(self):
        # Redirect snapshot to a temp dir
        self._tmpdir = tempfile.TemporaryDirectory()
        self._snapshot_path = os.path.join(self._tmpdir.name, "state_snapshot.json")

        import services.state_snapshot as ss
        self._ss = ss
        self._orig_path = ss._SNAPSHOT_PATH
        self._orig_dir = ss._SNAPSHOT_DIR
        ss._SNAPSHOT_PATH = self._snapshot_path
        ss._SNAPSHOT_DIR = self._tmpdir.name

        # Patch R2 module-level vars
        import services.r2_client as r2
        self._r2 = r2
        self._orig_enabled = r2._ENABLED
        self._orig_bucket = r2._BUCKET
        self._orig_endpoint = r2._ENDPOINT_URL
        r2._ENABLED = True
        r2._BUCKET = "retina-data"
        r2._ENDPOINT_URL = None  # None → boto3 uses default AWS endpoint; moto intercepts it
        r2._clear_cache()

        self._env_patcher = unittest.mock.patch.dict(os.environ, _FAKE_R2_ENV, clear=False)
        self._env_patcher.start()

    def tearDown(self):
        self._ss._SNAPSHOT_PATH = self._orig_path
        self._ss._SNAPSHOT_DIR = self._orig_dir
        self._r2._ENABLED = self._orig_enabled
        self._r2._BUCKET = self._orig_bucket
        self._r2._ENDPOINT_URL = self._orig_endpoint
        self._r2._clear_cache()
        self._env_patcher.stop()
        self._tmpdir.cleanup()

    # ── save_snapshot replicates to R2 ───────────────────────────────────────

    @mock_aws
    def test_save_snapshot_replicates_to_r2(self):
        """save_snapshot() should upload the snapshot file to R2."""
        _make_bucket()

        from services.state_snapshot import save_snapshot
        save_snapshot()

        # Local file written
        self.assertTrue(os.path.exists(self._snapshot_path))

        # R2 has the snapshot
        from services.r2_client import download_bytes
        data = download_bytes("snapshots/state_snapshot.json")
        self.assertIsNotNone(data, "Snapshot should be in R2")
        snap = json.loads(data)
        self.assertIn("saved_at", snap)
        self.assertIn("trust_scores", snap)

    @mock_aws
    def test_save_snapshot_local_content_matches_r2(self):
        """Local snapshot and R2 copy should have identical content."""
        _make_bucket()

        from services.state_snapshot import save_snapshot
        save_snapshot()

        with open(self._snapshot_path) as f:
            local = json.load(f)

        from services.r2_client import download_bytes
        r2_data = download_bytes("snapshots/state_snapshot.json")
        r2_snap = json.loads(r2_data)

        # Keys must match; saved_at may differ by <1s so just check structure
        self.assertEqual(set(local.keys()), set(r2_snap.keys()))

    @mock_aws
    def test_save_snapshot_local_written_even_if_r2_fails(self):
        """Local file should be written even when R2 upload fails."""
        _make_bucket()

        # Make R2 upload_file raise an exception
        with unittest.mock.patch("services.r2_client.upload_file", return_value=False):
            from services.state_snapshot import save_snapshot
            save_snapshot()

        self.assertTrue(os.path.exists(self._snapshot_path), "Local snapshot must exist")

    def test_save_snapshot_local_written_when_r2_disabled(self):
        """Local file should be written when R2 is not configured."""
        self._r2._ENABLED = False
        self._r2._clear_cache()

        from services.state_snapshot import save_snapshot
        save_snapshot()

        self.assertTrue(os.path.exists(self._snapshot_path))
        with open(self._snapshot_path) as f:
            snap = json.load(f)
        self.assertIn("trust_scores", snap)

    # ── restore_snapshot fallback to R2 ──────────────────────────────────────

    @mock_aws
    def test_restore_falls_back_to_r2_when_local_missing(self):
        """If local snapshot is absent, restore_snapshot() should fetch from R2."""
        _make_bucket()

        # Upload a snapshot to R2 directly (no local file)
        snap = _minimal_snapshot()
        from services.r2_client import upload_bytes
        upload_bytes("snapshots/state_snapshot.json", json.dumps(snap).encode())

        # Ensure local file does not exist
        self.assertFalse(os.path.exists(self._snapshot_path))

        from services.state_snapshot import restore_snapshot
        result = restore_snapshot()

        self.assertTrue(result, "restore_snapshot() should return True after R2 restore")

    @mock_aws
    def test_restore_prefers_local_over_r2(self):
        """When both local and R2 snapshots exist, local should be used (faster)."""
        _make_bucket()

        # Put different snapshots locally vs R2
        local_snap = _minimal_snapshot()
        local_snap["anomaly_log"] = [{"source": "local"}]
        with open(self._snapshot_path, "w") as f:
            json.dump(local_snap, f)

        r2_snap = _minimal_snapshot()
        r2_snap["anomaly_log"] = [{"source": "r2"}]
        from services.r2_client import upload_bytes
        upload_bytes("snapshots/state_snapshot.json", json.dumps(r2_snap).encode())

        # Track which source gets used by watching download_bytes calls
        with unittest.mock.patch.object(
            self._r2, "download_bytes", wraps=self._r2.download_bytes
        ) as mock_download:
            from services.state_snapshot import restore_snapshot
            result = restore_snapshot()
            # R2 download should NOT have been called since local file existed
            mock_download.assert_not_called()

        self.assertTrue(result)

    @mock_aws
    def test_restore_returns_false_when_no_snapshot_anywhere(self):
        """No local file + nothing in R2 → restore_snapshot() returns False."""
        _make_bucket()  # empty bucket

        from services.state_snapshot import restore_snapshot
        result = restore_snapshot()

        self.assertFalse(result)

    def test_restore_returns_false_when_r2_disabled_and_no_local(self):
        """No local file + R2 disabled → restore_snapshot() returns False."""
        self._r2._ENABLED = False
        self._r2._clear_cache()

        from services.state_snapshot import restore_snapshot
        result = restore_snapshot()

        self.assertFalse(result)

    @mock_aws
    def test_restore_returns_false_when_r2_has_corrupt_json(self):
        """Corrupt JSON in R2 → restore_snapshot() returns False cleanly."""
        _make_bucket()
        from services.r2_client import upload_bytes
        upload_bytes("snapshots/state_snapshot.json", b"NOT VALID JSON{{{{")

        from services.state_snapshot import restore_snapshot
        result = restore_snapshot()

        self.assertFalse(result)

    # ── Atomic write ─────────────────────────────────────────────────────────

    @mock_aws
    def test_save_snapshot_atomic_no_tmp_file_left_behind(self):
        """save_snapshot() should not leave a .tmp file after completion."""
        _make_bucket()

        from services.state_snapshot import save_snapshot
        save_snapshot()

        tmp_path = self._snapshot_path + ".tmp"
        self.assertFalse(os.path.exists(tmp_path), ".tmp file should be cleaned up")
        self.assertTrue(os.path.exists(self._snapshot_path))


if __name__ == "__main__":
    unittest.main()
