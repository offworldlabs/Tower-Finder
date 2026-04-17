"""Tests for archive lifecycle: offload to R2 + local disk cleanup.

Uses moto to mock S3/R2 in-process and a temporary directory for the
archive so the real archive is never touched.
"""

import os
import time
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import boto3
from moto import mock_aws


# ── Helpers ───────────────────────────────────────────────────────────────────

_FAKE_R2_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "AKIATEST",
    "R2_SECRET_ACCESS_KEY": "testsecret",
    "R2_BUCKET": "retina-data",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "testsecret",
}


def _make_bucket(bucket: str = "retina-data"):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)


def _create_archive_file(archive_dir: Path, age_days: float, content: bytes = b"{}") -> Path:
    """Create a fake .json archive file in year/month/day/node structure."""
    mtime = time.time() - age_days * 86400
    node_dir = archive_dir / "2025" / "01" / "01" / "node_test"
    node_dir.mkdir(parents=True, exist_ok=True)
    # Use unique names based on mtime so files don't collide
    fname = node_dir / f"frame_{int(mtime * 1000)}.json"
    fname.write_bytes(content)
    os.utime(fname, (mtime, mtime))
    return fname


# ── Fixtures ──────────────────────────────────────────────────────────────────

class TestArchiveLifecycle(unittest.TestCase):

    def setUp(self):
        # Temporary archive directory replaces the real one
        self._tmpdir = tempfile.TemporaryDirectory()
        self._archive_dir = Path(self._tmpdir.name) / "archive"
        self._archive_dir.mkdir()

        # Patch archive_lifecycle._ARCHIVE_DIR and env vars
        import services.tasks.archive_lifecycle as alc
        self._orig_archive_dir = alc._ARCHIVE_DIR
        alc._ARCHIVE_DIR = self._archive_dir

        self._env_patcher = unittest.mock.patch.dict(os.environ, _FAKE_R2_ENV, clear=False)
        self._env_patcher.start()

        # Patch r2_client module-level vars so is_enabled() returns True
        import services.r2_client as r2
        self._r2 = r2
        self._orig_enabled = r2._ENABLED
        self._orig_bucket = r2._BUCKET
        self._orig_endpoint = r2._ENDPOINT_URL
        r2._ENABLED = True
        r2._BUCKET = "retina-data"
        r2._ENDPOINT_URL = None  # None → boto3 uses default AWS endpoint; moto intercepts it
        r2._clear_cache()

    def tearDown(self):
        import services.tasks.archive_lifecycle as alc
        alc._ARCHIVE_DIR = self._orig_archive_dir

        self._r2._ENABLED = self._orig_enabled
        self._r2._BUCKET = self._orig_bucket
        self._r2._ENDPOINT_URL = self._orig_endpoint
        self._r2._clear_cache()

        self._env_patcher.stop()
        self._tmpdir.cleanup()

    # ── Phase 1: Upload old files to R2 ──────────────────────────────────────

    @mock_aws
    def test_old_file_is_uploaded_to_r2(self):
        """Files older than OFFLOAD_AGE_DAYS should be uploaded to R2."""
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=2.0)  # older than 1-day cutoff

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertGreater(stats["uploaded"], 0, "Expected at least 1 file uploaded to R2")
        self.assertEqual(stats["errors"], 0)

    @mock_aws
    def test_uploaded_file_content_in_r2(self):
        """Uploaded file should be retrievable from R2 with correct content."""
        _make_bucket()
        payload = b'{"node": "test", "detections": []}'
        f = _create_archive_file(self._archive_dir, age_days=2.0, content=payload)

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        from services.r2_client import list_keys, download_bytes
        run_archive_lifecycle()

        keys = list_keys("archive/")
        self.assertEqual(len(keys), 1)
        stored = download_bytes(keys[0])
        self.assertEqual(stored, payload)

    @mock_aws
    def test_fresh_file_not_uploaded(self):
        """Files younger than OFFLOAD_AGE_DAYS should NOT be uploaded."""
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=0.5)  # fresh — under 1-day cutoff

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertEqual(stats["uploaded"], 0)

    # ── Phase 2: Delete files past retention ─────────────────────────────────

    @mock_aws
    def test_very_old_file_deleted_locally(self):
        """Files older than RETENTION_DAYS should be removed from local disk."""
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=15.0)  # past 14-day retention

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertFalse(f.exists(), "File should be deleted from local disk")
        self.assertGreater(stats["deleted"], 0)

    @mock_aws
    def test_file_within_retention_not_deleted(self):
        """Files within retention window should survive on local disk."""
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=2.0)

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        run_archive_lifecycle()

        self.assertTrue(f.exists(), "File within retention period should NOT be deleted")

    @mock_aws
    def test_old_file_uploaded_then_deleted(self):
        """A file that is both >offload cutoff and >retention cutoff is uploaded AND deleted."""
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=15.0)  # both cutoffs

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertGreater(stats["uploaded"], 0, "Should be uploaded to R2")
        self.assertGreater(stats["deleted"], 0, "Should be deleted from local disk")
        self.assertFalse(f.exists(), "Should be gone from disk")

        from services.r2_client import list_keys
        keys = list_keys("archive/")
        self.assertEqual(len(keys), 1, "Should be in R2")

    # ── Phase 3: Empty directory pruning ─────────────────────────────────────

    @mock_aws
    def test_empty_dirs_pruned_after_delete(self):
        """After deleting all files in a directory, empty dirs should be removed."""
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=15.0)
        day_dir = f.parent.parent  # .../2025/01/01

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        run_archive_lifecycle()

        # The node dir (and day dir if empty) should be gone
        self.assertFalse(f.parent.exists(), "Empty node dir should be pruned")

    # ── R2 disabled ───────────────────────────────────────────────────────────

    def test_no_upload_when_r2_disabled(self):
        """When R2 is not configured, files should not be uploaded but old ones still deleted."""
        self._r2._ENABLED = False
        self._r2._clear_cache()

        f_old = _create_archive_file(self._archive_dir, age_days=15.0)

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertEqual(stats["uploaded"], 0, "No upload without R2")
        self.assertGreater(stats["deleted"], 0, "Local cleanup still happens")
        self.assertFalse(f_old.exists(), "Old file deleted even without R2")

    # ── Empty archive dir ─────────────────────────────────────────────────────

    @mock_aws
    def test_empty_archive_dir_returns_zero_stats(self):
        """An empty archive directory should return all-zero stats cleanly."""
        _make_bucket()
        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()
        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0})

    @mock_aws
    def test_nonexistent_archive_dir_returns_zero_stats(self):
        """Non-existent archive directory should return all-zero stats cleanly."""
        import services.tasks.archive_lifecycle as alc
        alc._ARCHIVE_DIR = self._archive_dir / "nonexistent"

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()
        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0})

    # ── Upload cap ────────────────────────────────────────────────────────────

    @mock_aws
    def test_upload_cap_respected(self):
        """Lifecycle should not upload more than _MAX_UPLOAD_PER_CYCLE files per cycle."""
        _make_bucket()
        import services.tasks.archive_lifecycle as alc

        # Create more files than the cap
        cap = alc._MAX_UPLOAD_PER_CYCLE
        for i in range(cap + 5):
            node_dir = self._archive_dir / "2025" / "01" / f"{i:02d}" / "node"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "frame.json"
            f.write_bytes(b"{}")
            mtime = time.time() - 2 * 86400
            os.utime(f, (mtime, mtime))

        from services.tasks.archive_lifecycle import run_archive_lifecycle
        stats = run_archive_lifecycle()

        self.assertLessEqual(stats["uploaded"], cap)


if __name__ == "__main__":
    unittest.main()
