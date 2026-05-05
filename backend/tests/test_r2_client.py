"""Tests for R2/S3 client using moto (in-process S3 mock).

These tests verify every public function of r2_client.py:
  upload_bytes, upload_file, download_bytes, list_keys,
  delete_key, delete_keys, is_enabled, _clear_cache.

moto intercepts all boto3 S3 calls in-process — no real server needed.
The R2 module-level env vars are patched so _ENABLED=True and the
lru_cache is cleared before/after each test so state doesn't leak.
"""

import os
import tempfile
import unittest
import unittest.mock

import boto3
from moto import mock_aws

# ── Helpers ───────────────────────────────────────────────────────────────────

# Fake credentials required by moto (any non-empty values work)
_FAKE_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "AKIATEST",
    "R2_SECRET_ACCESS_KEY": "testsecret",
    "R2_BUCKET": "retina-data",
    # moto intercepts ALL boto3 calls to AWS endpoints, including custom ones,
    # so we don't set R2_ENDPOINT_URL — the default endpoint is intercepted.
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "testsecret",
}


def _make_bucket(bucket_name: str = "retina-data"):
    """Create a fresh S3 bucket inside an active moto mock context."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket_name)


# ── Fixtures / setup ──────────────────────────────────────────────────────────

class TestR2Client(unittest.TestCase):
    """Unit tests for r2_client.py — each test gets a fresh moto context."""

    def setUp(self):
        """Patch env vars and clear the lru_cache before every test."""
        self._env_patcher = unittest.mock.patch.dict(os.environ, _FAKE_ENV, clear=False)
        self._env_patcher.start()

        # Re-import to pick up patched env vars, then clear cache
        import services.r2_client as r2
        # Patch the module-level _ENABLED and _BUCKET so they see test values
        self._orig_enabled = r2._ENABLED
        self._orig_bucket = r2._BUCKET
        self._orig_account = r2._ACCOUNT_ID
        self._orig_access = r2._ACCESS_KEY
        self._orig_secret = r2._SECRET_KEY
        self._orig_endpoint = r2._ENDPOINT_URL

        r2._ENABLED = True
        r2._BUCKET = "retina-data"
        r2._ACCOUNT_ID = "testaccount"
        r2._ACCESS_KEY = "AKIATEST"
        r2._SECRET_KEY = "testsecret"
        r2._ENDPOINT_URL = None  # None → boto3 uses default AWS endpoint; moto intercepts it
        r2._clear_cache()

    def tearDown(self):
        import services.r2_client as r2
        r2._ENABLED = self._orig_enabled
        r2._BUCKET = self._orig_bucket
        r2._ACCOUNT_ID = self._orig_account
        r2._ACCESS_KEY = self._orig_access
        r2._SECRET_KEY = self._orig_secret
        r2._ENDPOINT_URL = self._orig_endpoint
        r2._clear_cache()
        self._env_patcher.stop()

    # ── is_enabled ────────────────────────────────────────────────────────────

    @mock_aws
    def test_is_enabled_true(self):
        import services.r2_client as r2
        self.assertTrue(r2.is_enabled())

    def test_is_enabled_false_when_no_creds(self):
        import services.r2_client as r2
        r2._ENABLED = False
        self.assertFalse(r2.is_enabled())

    # ── upload_bytes ──────────────────────────────────────────────────────────

    @mock_aws
    def test_upload_bytes_success(self):
        _make_bucket()
        import services.r2_client as r2
        ok = r2.upload_bytes("test/hello.json", b'{"hello": "world"}')
        self.assertTrue(ok)

    @mock_aws
    def test_upload_bytes_content_is_stored(self):
        _make_bucket()
        import services.r2_client as r2
        payload = b'{"answer": 42}'
        r2.upload_bytes("test/data.json", payload)
        result = r2.download_bytes("test/data.json")
        self.assertEqual(result, payload)

    def test_upload_bytes_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        ok = r2.upload_bytes("test/x.json", b"data")
        self.assertFalse(ok)

    # ── upload_file ───────────────────────────────────────────────────────────

    @mock_aws
    def test_upload_file_success(self):
        _make_bucket()
        import services.r2_client as r2
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"snapshot": true}')
            tmp_path = f.name
        try:
            ok = r2.upload_file("snapshots/state.json", tmp_path)
            self.assertTrue(ok)
            # Verify content
            result = r2.download_bytes("snapshots/state.json")
            self.assertEqual(result, b'{"snapshot": true}')
        finally:
            os.unlink(tmp_path)

    def test_upload_file_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        ok = r2.upload_file("k", "/nonexistent/path")
        self.assertFalse(ok)

    # ── download_bytes ────────────────────────────────────────────────────────

    @mock_aws
    def test_download_bytes_returns_none_for_missing_key(self):
        _make_bucket()
        import services.r2_client as r2
        result = r2.download_bytes("does/not/exist.json")
        self.assertIsNone(result)

    @mock_aws
    def test_download_bytes_round_trip(self):
        _make_bucket()
        import services.r2_client as r2
        data = b'{"key": "value", "n": 123}'
        r2.upload_bytes("round/trip.json", data)
        result = r2.download_bytes("round/trip.json")
        self.assertEqual(result, data)

    def test_download_bytes_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        self.assertIsNone(r2.download_bytes("any/key"))

    # ── list_keys ─────────────────────────────────────────────────────────────

    @mock_aws
    def test_list_keys_empty_bucket(self):
        _make_bucket()
        import services.r2_client as r2
        keys = r2.list_keys("archive/")
        self.assertEqual(keys, [])

    @mock_aws
    def test_list_keys_with_prefix(self):
        _make_bucket()
        import services.r2_client as r2
        r2.upload_bytes("archive/2025/01/file1.json", b"1")
        r2.upload_bytes("archive/2025/02/file2.json", b"2")
        r2.upload_bytes("snapshots/state.json", b"3")
        keys = r2.list_keys("archive/")
        self.assertEqual(sorted(keys), [
            "archive/2025/01/file1.json",
            "archive/2025/02/file2.json",
        ])

    @mock_aws
    def test_list_keys_no_prefix(self):
        _make_bucket()
        import services.r2_client as r2
        r2.upload_bytes("a.json", b"a")
        r2.upload_bytes("b.json", b"b")
        keys = r2.list_keys()
        self.assertEqual(sorted(keys), ["a.json", "b.json"])

    def test_list_keys_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        self.assertEqual(r2.list_keys(), [])

    # ── delete_key ────────────────────────────────────────────────────────────

    @mock_aws
    def test_delete_key_removes_object(self):
        _make_bucket()
        import services.r2_client as r2
        r2.upload_bytes("to_delete.json", b"bye")
        ok = r2.delete_key("to_delete.json")
        self.assertTrue(ok)
        self.assertIsNone(r2.download_bytes("to_delete.json"))

    @mock_aws
    def test_delete_key_nonexistent_returns_true(self):
        # S3 delete_object on missing key is idempotent (returns 204)
        _make_bucket()
        import services.r2_client as r2
        ok = r2.delete_key("ghost.json")
        self.assertTrue(ok)

    def test_delete_key_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        self.assertFalse(r2.delete_key("x"))

    # ── delete_keys ───────────────────────────────────────────────────────────

    @mock_aws
    def test_delete_keys_bulk(self):
        _make_bucket()
        import services.r2_client as r2
        keys = [f"file/{i}.json" for i in range(10)]
        for k in keys:
            r2.upload_bytes(k, b"x")
        deleted = r2.delete_keys(keys)
        self.assertEqual(deleted, 10)
        # Verify all gone
        remaining = r2.list_keys("file/")
        self.assertEqual(remaining, [])

    @mock_aws
    def test_delete_keys_empty_list(self):
        _make_bucket()
        import services.r2_client as r2
        self.assertEqual(r2.delete_keys([]), 0)

    def test_delete_keys_noop_when_disabled(self):
        import services.r2_client as r2
        r2._ENABLED = False
        r2._clear_cache()
        self.assertEqual(r2.delete_keys(["a", "b"]), 0)

    # ── Exception paths (boto3 call raises) ──────────────────────────────────

    def _make_failing_client(self, method: str, r2):
        """Return a patched _get_client whose named method raises."""
        mock_client = unittest.mock.MagicMock()
        getattr(mock_client, method).side_effect = Exception("boto3 error")
        # download_bytes catches client.exceptions.NoSuchKey before Exception;
        # give it a real exception subclass so Python can evaluate the except clause.
        mock_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return unittest.mock.patch.object(r2, "_get_client", return_value=mock_client)

    def test_upload_bytes_exception_returns_false(self):
        import services.r2_client as r2
        with self._make_failing_client("put_object", r2):
            self.assertFalse(r2.upload_bytes("k", b"data"))

    def test_upload_file_exception_returns_false(self):
        import services.r2_client as r2
        with self._make_failing_client("upload_file", r2):
            self.assertFalse(r2.upload_file("k", "/some/path"))

    def test_download_bytes_exception_returns_none(self):
        import services.r2_client as r2
        with self._make_failing_client("get_object", r2):
            self.assertIsNone(r2.download_bytes("k"))

    def test_list_keys_exception_returns_empty(self):
        import services.r2_client as r2
        with self._make_failing_client("get_paginator", r2):
            self.assertEqual(r2.list_keys(), [])

    def test_delete_key_exception_returns_false(self):
        import services.r2_client as r2
        with self._make_failing_client("delete_object", r2):
            self.assertFalse(r2.delete_key("k"))

    def test_delete_keys_exception_returns_zero(self):
        import services.r2_client as r2
        with self._make_failing_client("delete_objects", r2):
            self.assertEqual(r2.delete_keys(["a", "b"]), 0)

    def test_get_client_exception_returns_none(self):
        """boto3.client() itself raises → _get_client returns None."""
        import services.r2_client as r2
        with unittest.mock.patch("boto3.client", side_effect=Exception("no boto3")):
            r2._clear_cache()
            result = r2._get_client()
        self.assertIsNone(result)
        r2._clear_cache()


# ── Allow running directly ────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main()
