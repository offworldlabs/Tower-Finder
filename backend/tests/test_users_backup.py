"""Tests for users.db daily backup to R2 + retention pruning."""

import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _make_bucket(bucket: str = "retina-data"):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


class _UsersBackupBase(unittest.TestCase):
    """Sets up an isolated R2 bucket and a fake users.db on disk."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "users.db"
        _seed_db(self._db_path)

        self._env_patcher = unittest.mock.patch.dict(
            os.environ, _FAKE_R2_ENV, clear=False,
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

        # Point the backup task at our fake users.db.
        self._db_patcher = unittest.mock.patch(
            "services.tasks.users_backup._users_db_path",
            return_value=self._db_path,
        )
        self._db_patcher.start()

    def tearDown(self):
        self._db_patcher.stop()
        self._r2._ENABLED = self._orig_enabled
        self._r2._BUCKET = self._orig_bucket
        self._r2._ENDPOINT_URL = self._orig_endpoint
        self._r2._clear_cache()
        self._env_patcher.stop()
        self._tmpdir.cleanup()


class TestUsersBackup(_UsersBackupBase):
    @mock_aws
    def test_uploads_today_backup(self):
        _make_bucket()
        from services.r2_client import list_keys
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()
        today = datetime.now(timezone.utc).date().isoformat()
        expected_key = f"backups/users-db/{today}.db"

        self.assertEqual(stats["uploaded"], expected_key)
        self.assertEqual(stats["skipped"], None)
        self.assertIn(expected_key, list_keys("backups/users-db/"))

    @mock_aws
    def test_uploaded_blob_is_a_valid_sqlite_db(self):
        """VACUUM INTO must produce a self-consistent file we can re-open."""
        _make_bucket()
        from services.r2_client import download_bytes
        from services.tasks.users_backup import run_users_db_backup

        run_users_db_backup()
        today = datetime.now(timezone.utc).date().isoformat()
        blob = download_bytes(f"backups/users-db/{today}.db")
        self.assertIsNotNone(blob)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(blob)
            restored_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(restored_path))
            try:
                rows = list(conn.execute("SELECT v FROM t"))
            finally:
                conn.close()
            self.assertEqual(rows, [("hello",)])
        finally:
            restored_path.unlink(missing_ok=True)

    @mock_aws
    def test_retention_drops_old_backups(self):
        """Anything older than RETENTION_DAYS (counted from the date in the
        key, not R2 LastModified) must be deleted by the same cycle.
        """
        _make_bucket()
        from services.r2_client import list_keys, upload_bytes
        from services.tasks.users_backup import run_users_db_backup

        # Plant one backup well outside the retention window and one inside.
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
        upload_bytes(f"backups/users-db/{old_date}.db", b"old", content_type="application/x-sqlite3")
        upload_bytes(f"backups/users-db/{recent_date}.db", b"recent", content_type="application/x-sqlite3")

        stats = run_users_db_backup()

        keys = list_keys("backups/users-db/")
        self.assertNotIn(f"backups/users-db/{old_date}.db", keys, "old backup must be pruned")
        self.assertIn(f"backups/users-db/{recent_date}.db", keys, "recent backup must survive")
        self.assertGreaterEqual(stats["deleted"], 1)

    def test_skipped_when_r2_disabled(self):
        self._r2._ENABLED = False
        self._r2._clear_cache()
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()

        self.assertEqual(stats["skipped"], "r2-disabled")
        self.assertIsNone(stats["uploaded"])

    @mock_aws
    def test_skipped_when_db_missing(self):
        _make_bucket()
        self._db_path.unlink()  # delete the fake DB
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()

        self.assertEqual(stats["skipped"], "db-missing")

    @mock_aws
    def test_idempotent_within_same_day(self):
        """Running the backup twice on the same day overwrites the same key
        and counts as one entry (no duplicates to prune)."""
        _make_bucket()
        from services.r2_client import list_keys
        from services.tasks.users_backup import run_users_db_backup

        run_users_db_backup()
        run_users_db_backup()

        today = datetime.now(timezone.utc).date().isoformat()
        keys = [k for k in list_keys("backups/users-db/") if k.endswith(f"{today}.db")]
        self.assertEqual(len(keys), 1, "second run must overwrite, not duplicate")


if __name__ == "__main__":
    unittest.main()
