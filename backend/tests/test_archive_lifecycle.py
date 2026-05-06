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

from config.constants import ARCHIVE_OFFLOAD_AGE_DAYS

# Tests that exercise the deletion phase patch this value into the lifecycle
# module — the production default is 0 (disabled), so we use a fixed positive
# value here whenever we need to verify that deletion happens.
_TEST_RETENTION_DAYS = 14


def _patch_retention(value: int = _TEST_RETENTION_DAYS):
    """Patch ARCHIVE_RETENTION_DAYS inside the lifecycle module for one test."""
    return unittest.mock.patch(
        "services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", value
    )

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
            self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
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

        with _patch_retention():
            run_archive_lifecycle()
        self.assertTrue(f.exists())

    @mock_aws
    def test_old_file_uploaded_then_deleted(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1
        )
        from services.r2_client import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f.exists())
        self.assertEqual(len(list_keys("archive/")), 1)

    @mock_aws
    def test_empty_dirs_pruned_after_delete(self):
        _make_bucket()
        f = _create_archive_file(
            self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            run_archive_lifecycle()
        self.assertFalse(f.parent.exists())

    def test_no_upload_when_r2_disabled(self):
        """When R2 is disabled, no upload happens AND no local deletion either.

        Local files are only deleted once they have an .uploaded sentinel,
        which is only written after a confirmed R2 upload. Without R2, the
        sentinel never appears, so the file must stay on disk indefinitely —
        otherwise we'd silently drop data with no backup at all.
        """
        self._r2._ENABLED = False
        self._r2._clear_cache()
        f_old = _create_archive_file(
            self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1
        )
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertEqual(stats["uploaded"], 0)
        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(
            f_old.exists(),
            "file must NOT be deleted when R2 never confirmed the upload",
        )

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

    def _run(self, r2_enabled: bool = True, upload_return: bool = True):
        """Run archive lifecycle with r2_client fully mocked.

        Defaults reflect the happy path (R2 reachable, uploads succeed) so a
        test exercising the deletion phase doesn't have to repeat that setup.
        Tests of the R2-disabled or upload-failure paths pass explicit args.
        """
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
        age = _TEST_RETENTION_DAYS + 1
        _make_archive_file(self._archive_dir, age_days=age)

        original_unlink = Path.unlink

        def _bad_unlink(path, missing_ok=False):
            if path.suffix == ".json":
                raise OSError("fake unlink error")
            return original_unlink(path, missing_ok=missing_ok)

        with unittest.mock.patch.object(Path, "unlink", _bad_unlink), _patch_retention():
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

        age = _TEST_RETENTION_DAYS + 1
        for i in range(cap + 3):
            node_dir = self._archive_dir / "2024" / "01" / "15" / f"node-{i}"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "data.json"
            f.write_bytes(b"{}")
            mtime = time.time() - age * 86400
            os.utime(f, (mtime, mtime))
            # Pre-mark as uploaded so the deletion phase has work to do all
            # the way to the cap (otherwise we'd hit the upload sub-cap of
            # _MAX_UPLOAD_PER_CYCLE first and stop short).
            f.with_name(f.name + ".uploaded").touch()

        with _patch_retention():
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

    def test_parquet_and_json_files_are_yielded(self):
        """Both .parquet and .json files in the node dir are yielded; .txt is ignored."""
        node_dir = self._archive_dir / "2024" / "01" / "15" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "data.json").write_bytes(b"{}")
        (node_dir / "part-120000.parquet").write_bytes(b"")
        (node_dir / "data.txt").write_bytes(b"ignore me")

        results = self._collect()
        names = sorted(p.name for p in results)
        self.assertEqual(names, ["data.json", "part-120000.parquet"])

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


# ── TestRetentionDisabled ─────────────────────────────────────────────────────


class TestRetentionDisabled(_ArchiveUnitTestBase):
    """When ARCHIVE_RETENTION_DAYS <= 0 the deletion phase is skipped entirely."""

    def test_old_file_kept_when_retention_zero(self):
        f = _make_archive_file(self._archive_dir, age_days=400.0)

        with unittest.mock.patch(
            "services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", 0
        ):
            stats = self._run()

        self.assertTrue(f.exists(), "file must NOT be deleted when retention is 0")
        self.assertEqual(stats["deleted"], 0)

    def test_old_file_kept_when_retention_negative(self):
        f = _make_archive_file(self._archive_dir, age_days=400.0)

        with unittest.mock.patch(
            "services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", -1
        ):
            stats = self._run()

        self.assertTrue(f.exists(), "negative retention must also disable deletion")
        self.assertEqual(stats["deleted"], 0)

    def test_old_file_deleted_when_retention_positive(self):
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        with _patch_retention():
            stats = self._run()

        self.assertFalse(f.exists(), "file must be deleted when retention is positive")
        self.assertEqual(stats["deleted"], 1)


# ── TestUploadSentinel ────────────────────────────────────────────────────────


class TestUploadSentinel(_ArchiveUnitTestBase):
    """Sentinel file (.uploaded) gates local deletion on confirmed R2 upload."""

    def test_sentinel_created_on_successful_upload(self):
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(r2_enabled=True, upload_return=True)

        self.assertEqual(stats["uploaded"], 1)
        sentinel = f.with_name(f.name + ".uploaded")
        self.assertTrue(sentinel.exists(), "sentinel must be written after R2 upload")

    def test_sentinel_not_created_on_failed_upload(self):
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(r2_enabled=True, upload_return=False)

        self.assertEqual(stats["uploaded"], 0)
        self.assertGreater(stats["errors"], 0)
        sentinel = f.with_name(f.name + ".uploaded")
        self.assertFalse(sentinel.exists(), "sentinel must NOT exist on upload failure")

    def test_existing_sentinel_skips_re_upload(self):
        """A second cycle must not waste an R2 PUT on a file already uploaded."""
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        f.with_name(f.name + ".uploaded").touch()

        with unittest.mock.patch(
            "services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir
        ), unittest.mock.patch(
            "services.r2_client.is_enabled", return_value=True
        ), unittest.mock.patch(
            "services.r2_client.upload_file", return_value=True
        ) as mock_upload:
            from services.tasks.archive_lifecycle import run_archive_lifecycle
            stats = run_archive_lifecycle()

        mock_upload.assert_not_called()
        self.assertEqual(stats["uploaded"], 0)

    def test_file_without_sentinel_not_deleted(self):
        """A file past retention but lacking a sentinel must NOT be deleted."""
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        # R2 disabled — sentinel will never be created, simulating a deployment
        # where R2 is misconfigured but RETENTION_DAYS was set positive.
        with _patch_retention():
            stats = self._run(r2_enabled=False, upload_return=False)

        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(f.exists(), "file without sentinel must survive lifecycle")

    def test_sentinel_removed_with_file(self):
        """When the file is deleted, its sentinel must go too (no orphan)."""
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        with _patch_retention():
            stats = self._run(r2_enabled=True, upload_return=True)

        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(f.exists())
        self.assertFalse(
            f.with_name(f.name + ".uploaded").exists(),
            "sentinel must be removed alongside its file",
        )


if __name__ == "__main__":
    unittest.main()
