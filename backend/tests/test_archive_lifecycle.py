"""Tests for archive lifecycle: offload to R2 + local disk cleanup.

Two suites:
  1. TestArchiveLifecycleMoto  — integration tests using moto to mock S3/R2.
  2. Unit test classes         — error-path and edge-case tests using
                                 unittest.mock only (no real or mocked S3).
"""

import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import boto3
from moto import mock_aws

from config.constants import ARCHIVE_OFFLOAD_AGE_DAYS, ARCHIVE_RETENTION_DAYS

# ── Shared helpers ─────────────────────────────────────────────────────────────

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


def _create_archive_file(
    archive_dir: Path, age_days: float, content: bytes = b"{}"
) -> Path:
    """Create a year/month/day/node/frame_*.json file with the given mtime age."""
    mtime = time.time() - age_days * 86400
    node_dir = archive_dir / "2025" / "01" / "01" / "node_test"
    node_dir.mkdir(parents=True, exist_ok=True)
    fname = node_dir / f"frame_{int(mtime * 1000)}.json"
    fname.write_bytes(content)
    os.utime(fname, (mtime, mtime))
    return fname


def _make_archive_file(
    archive_dir: Path, age_days: float = 0.0, name: str = "data.json"
) -> Path:
    """Create a properly structured archive file used by the unit-test suite."""
    node_dir = archive_dir / "2024" / "01" / "15" / "node-A"
    node_dir.mkdir(parents=True, exist_ok=True)
    f = node_dir / name
    f.write_bytes(b"{}")
    mtime = time.time() - age_days * 86400
    os.utime(f, (mtime, mtime))
    return f


# ══════════════════════════════════════════════════════════════════════════════
# Suite 1: moto-based integration tests
# ══════════════════════════════════════════════════════════════════════════════


class TestArchiveLifecycleMoto(unittest.TestCase):
    """Archive lifecycle integration tests using moto to mock S3/R2."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._archive_dir = Path(self._tmpdir.name) / "archive"
        self._archive_dir.mkdir()

        import services.tasks.archive_lifecycle as alc

        self._orig_archive_dir = alc._ARCHIVE_DIR
        alc._ARCHIVE_DIR = self._archive_dir

        self._env_patcher = unittest.mock.patch.dict(
            os.environ, _FAKE_R2_ENV, clear=False
        )
        self._env_patcher.start()

        import services.r2_client as r2

        self._r2 = r2
        self._orig_enabled = r2._ENABLED
        self._orig_bucket = r2._BUCKET
        self._orig_endpoint = r2._ENDPOINT_URL
        r2._ENABLED = True
        r2._BUCKET = "retina-data"
        r2._ENDPOINT_URL = None
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

    @mock_aws
    def test_old_file_is_uploaded_to_r2(self):
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        self.assertEqual(stats["errors"], 0)

    @mock_aws
    def test_uploaded_file_content_in_r2(self):
        _make_bucket()
        payload = b'{"node": "test", "detections": []}'
        _create_archive_file(
            self._archive_dir,
            age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1,
            content=payload,
        )
        from services.r2_client import download_bytes, list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        run_archive_lifecycle()
        keys = list_keys("archive/")
        self.assertEqual(len(keys), 1)
        stored = download_bytes(keys[0])
        self.assertEqual(stored, payload)

    @mock_aws
    def test_fresh_file_not_uploaded(self):
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=0.5)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual(stats["uploaded"], 0)

    @mock_aws
    def test_very_old_file_deleted_locally(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=ARCHIVE_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertFalse(f.exists())
        self.assertGreater(stats["deleted"], 0)

    @mock_aws
    def test_file_within_retention_not_deleted(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        run_archive_lifecycle()
        self.assertTrue(f.exists())

    @mock_aws
    def test_old_file_uploaded_then_deleted(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=ARCHIVE_RETENTION_DAYS + 1
        )
        from services.r2_client import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f.exists())
        self.assertEqual(len(list_keys("archive/")), 1)

    @mock_aws
    def test_empty_dirs_pruned_after_delete(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=ARCHIVE_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        run_archive_lifecycle()
        self.assertFalse(f.parent.exists())

    def test_no_upload_when_r2_disabled(self):
        self._r2._ENABLED = False
        self._r2._clear_cache()
        f_old = _create_archive_file(
            self._archive_dir, age_days=ARCHIVE_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual(stats["uploaded"], 0)
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f_old.exists())

    @mock_aws
    def test_empty_archive_dir_returns_zero_stats(self):
        _make_bucket()
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual(
            stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0}
        )

    @mock_aws
    def test_upload_cap_respected(self):
        _make_bucket()
        import services.tasks.archive_lifecycle as alc

        cap = alc._MAX_UPLOAD_PER_CYCLE
        for i in range(cap + 5):
            node_dir = self._archive_dir / "2025" / "01" / f"{i:02d}" / "node"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "frame.json"
            f.write_bytes(b"{}")
            mtime = time.time() - (ARCHIVE_OFFLOAD_AGE_DAYS + 1) * 86400
            os.utime(f, (mtime, mtime))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertLessEqual(stats["uploaded"], cap)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 2: unit tests for error paths and edge cases (no moto required)
# ══════════════════════════════════════════════════════════════════════════════


class _ArchiveUnitTestBase(unittest.TestCase):
    """Shared setUp/tearDown for unit-test classes that need a scratch archive dir."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._archive_dir = Path(self._tmpdir.name) / "archive"
        self._archive_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, r2_enabled: bool = False, upload_return: bool = False):
        """Run archive lifecycle with r2_client fully mocked."""
        with unittest.mock.patch(
            "services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir
        ):
            with unittest.mock.patch(
                "services.r2_client.is_enabled", return_value=r2_enabled
            ):
                with unittest.mock.patch(
                    "services.r2_client.upload_file", return_value=upload_return
                ):
                    from services.tasks.archive_lifecycle import run_archive_lifecycle

                    return run_archive_lifecycle()


# ── TestRunArchiveLifecycleEmptyDir ───────────────────────────────────────────


class TestRunArchiveLifecycleEmptyDir(unittest.TestCase):
    """_ARCHIVE_DIR does not exist → all-zero stats, no crash."""

    def test_nonexistent_dir_returns_zero_stats(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no_such_archive"
            # missing is NOT created

            with unittest.mock.patch(
                "services.tasks.archive_lifecycle._ARCHIVE_DIR", new=missing
            ):
                from services.tasks.archive_lifecycle import run_archive_lifecycle

                with unittest.mock.patch(
                    "services.r2_client.is_enabled", return_value=False
                ):
                    with unittest.mock.patch(
                        "services.r2_client.upload_file", return_value=False
                    ):
                        stats = run_archive_lifecycle()

        self.assertEqual(
            stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0}
        )


# ── TestRunArchiveLifecycleOSError ────────────────────────────────────────────


class TestRunArchiveLifecycleOSError(_ArchiveUnitTestBase):
    """OSError paths: stat() skip, unlink() error."""

    def test_stat_oserror_skips_file_no_error_counted(self):
        """When stat() raises OSError, the file is silently skipped."""
        _make_archive_file(self._archive_dir, age_days=30.0)

        original_stat = Path.stat

        def _bad_stat(path, *args, **kwargs):
            if path.suffix == ".json":
                raise OSError("fake stat error")
            return original_stat(path, *args, **kwargs)

        with unittest.mock.patch.object(Path, "stat", _bad_stat):
            stats = self._run()

        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["uploaded"], 0)
        self.assertEqual(stats["deleted"], 0)

    def test_unlink_oserror_increments_errors(self):
        """When unlink() raises OSError on a file past retention, errors is incremented."""
        age = ARCHIVE_RETENTION_DAYS + 1
        _make_archive_file(self._archive_dir, age_days=age)

        original_unlink = Path.unlink

        def _bad_unlink(path, missing_ok=False):
            if path.suffix == ".json":
                raise OSError("fake unlink error")
            return original_unlink(path, missing_ok=missing_ok)

        with unittest.mock.patch.object(Path, "unlink", _bad_unlink):
            stats = self._run()

        self.assertGreater(stats["errors"], 0)
        self.assertEqual(stats["deleted"], 0)

    def test_upload_failure_increments_errors(self):
        """When r2_upload() returns False (upload failed silently), errors is incremented."""
        _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(r2_enabled=True, upload_return=False)

        self.assertGreater(stats["errors"], 0)
        self.assertEqual(stats["uploaded"], 0)


# ── TestRunArchiveLifecycleCap ────────────────────────────────────────────────


class TestRunArchiveLifecycleCap(_ArchiveUnitTestBase):
    """When uploaded+deleted >= _MAX_DELETE_PER_CYCLE, extras get skipped."""

    def test_files_beyond_cap_are_skipped(self):
        import services.tasks.archive_lifecycle as alc

        cap = alc._MAX_DELETE_PER_CYCLE

        age = ARCHIVE_RETENTION_DAYS + 1
        for i in range(cap + 3):
            node_dir = self._archive_dir / "2024" / "01" / "15" / f"node-{i}"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "data.json"
            f.write_bytes(b"{}")
            mtime = time.time() - age * 86400
            os.utime(f, (mtime, mtime))

        stats = self._run()

        self.assertGreater(stats["skipped"], 0)
        self.assertLessEqual(stats["uploaded"] + stats["deleted"], cap)


# ── TestPruneEmptyDirs ────────────────────────────────────────────────────────


class TestPruneEmptyDirs(unittest.TestCase):
    """_prune_empty_dirs behaviour: empty leaf removed, file-containing dir kept, base kept."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name) / "base"
        self._base.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_leaf_dir_is_removed(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        leaf = self._base / "2024" / "01" / "15"
        leaf.mkdir(parents=True)

        _prune_empty_dirs(self._base)

        self.assertFalse(leaf.exists(), "Empty leaf dir should be pruned")

    def test_dir_with_files_is_kept(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        leaf = self._base / "2024" / "01" / "15"
        leaf.mkdir(parents=True)
        (leaf / "data.json").write_bytes(b"{}")

        _prune_empty_dirs(self._base)

        self.assertTrue(leaf.exists(), "Dir containing files should NOT be pruned")
        self.assertTrue((leaf / "data.json").exists())

    def test_base_dir_is_never_removed(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        _prune_empty_dirs(self._base)

        self.assertTrue(self._base.exists(), "Base directory must never be removed")


# ── TestIterArchiveFiles ──────────────────────────────────────────────────────


class TestIterArchiveFiles(_ArchiveUnitTestBase):
    """_iter_archive_files: non-dirs skipped, OSError doesn't crash."""

    def _collect(self):
        from services.tasks.archive_lifecycle import _iter_archive_files

        with unittest.mock.patch(
            "services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir
        ):
            return list(_iter_archive_files())

    def test_non_dir_entries_at_each_level_are_skipped(self):
        """Files placed at year/month/day/node level (not in a dir) are not yielded."""
        (self._archive_dir / "notadir.json").write_bytes(b"{}")
        _make_archive_file(self._archive_dir, name="valid.json")

        results = self._collect()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "valid.json")

    def test_only_json_files_are_yielded(self):
        """Non-.json files in the node dir are not yielded."""
        node_dir = self._archive_dir / "2024" / "01" / "15" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "data.json").write_bytes(b"{}")
        (node_dir / "data.txt").write_bytes(b"ignore me")

        results = self._collect()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "data.json")

    def test_oserror_during_iteration_does_not_crash(self):
        """If iterdir() raises OSError, _iter_archive_files returns empty gracefully."""
        _make_archive_file(self._archive_dir)

        with unittest.mock.patch.object(
            Path,
            "iterdir",
            side_effect=OSError("fake iterdir failure"),
        ):
            results = self._collect()

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
