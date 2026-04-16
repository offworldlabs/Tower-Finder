"""Cloudflare R2 (S3-compatible) object storage client.

Provides upload, download, list, and delete operations against an R2 bucket.
Falls back gracefully when credentials are not configured — all operations
become no-ops so the server runs fine without R2.

Requires env vars:
    R2_ACCOUNT_ID       — Cloudflare account ID
    R2_ACCESS_KEY_ID    — S3-compatible access key
    R2_SECRET_ACCESS_KEY — S3-compatible secret key
    R2_BUCKET           — Bucket name (default: "retina-data")
"""

import logging
import os
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID", "")
_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
_BUCKET = os.getenv("R2_BUCKET", "retina-data")
# Optional: override endpoint for local dev (MinIO, moto-server)
# e.g. R2_ENDPOINT_URL=http://localhost:9000
_ENDPOINT_URL = os.getenv(
    "R2_ENDPOINT_URL",
    f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com" if _ACCOUNT_ID else "",
)

_ENABLED = bool(_ACCOUNT_ID and _ACCESS_KEY and _SECRET_KEY)


@lru_cache(maxsize=1)
def _get_client():
    """Lazy-init boto3 S3 client for R2. Returns None if not configured."""
    if not _ENABLED:
        return None
    try:
        import boto3
        from botocore.config import Config

        endpoint = _ENDPOINT_URL or None  # boto3 rejects empty string
        region = "auto" if endpoint else "us-east-1"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
            config=Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=30,
            ),
            region_name=region,
        )
        logger.info("R2 client initialised (bucket=%s)", _BUCKET)
        return client
    except Exception:
        logger.exception("Failed to initialise R2 client")
        return None


def is_enabled() -> bool:
    """True if R2 credentials are configured."""
    return _ENABLED


def upload_bytes(key: str, data: bytes, content_type: str = "application/json") -> bool:
    """Upload raw bytes to R2. Returns True on success."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.put_object(Bucket=_BUCKET, Key=key, Body=data, ContentType=content_type)
        return True
    except Exception:
        logger.exception("R2 upload failed: %s", key)
        return False


def upload_file(key: str, file_path: str, content_type: str = "application/json") -> bool:
    """Upload a local file to R2. Returns True on success."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.upload_file(file_path, _BUCKET, key, ExtraArgs={"ContentType": content_type})
        return True
    except Exception:
        logger.exception("R2 upload_file failed: %s → %s", file_path, key)
        return False


def download_bytes(key: str) -> Optional[bytes]:
    """Download an object from R2. Returns bytes or None on failure."""
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=_BUCKET, Key=key)
        return resp["Body"].read()
    except client.exceptions.NoSuchKey:
        return None
    except Exception:
        logger.exception("R2 download failed: %s", key)
        return None


def list_keys(prefix: str = "", max_keys: int = 1000) -> list[str]:
    """List object keys under a prefix. Returns empty list on failure."""
    client = _get_client()
    if client is None:
        return []
    try:
        resp = client.list_objects_v2(Bucket=_BUCKET, Prefix=prefix, MaxKeys=max_keys)
        return [obj["Key"] for obj in resp.get("Contents", [])]
    except Exception:
        logger.exception("R2 list failed: prefix=%s", prefix)
        return []


def delete_key(key: str) -> bool:
    """Delete an object from R2. Returns True on success."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.delete_object(Bucket=_BUCKET, Key=key)
        return True
    except Exception:
        logger.exception("R2 delete failed: %s", key)
        return False


def delete_keys(keys: list[str]) -> int:
    """Bulk-delete up to 1000 keys. Returns count of successfully deleted."""
    client = _get_client()
    if client is None:
        return 0
    if not keys:
        return 0
    try:
        # S3 DeleteObjects accepts max 1000 per call
        batch = [{"Key": k} for k in keys[:1000]]
        resp = client.delete_objects(Bucket=_BUCKET, Delete={"Objects": batch, "Quiet": True})
        errors = resp.get("Errors", [])
        return len(batch) - len(errors)
    except Exception:
        logger.exception("R2 bulk delete failed (%d keys)", len(keys))
        return 0


def _clear_cache() -> None:
    """Clear the cached boto3 client. Used in tests to reset state between runs."""
    _get_client.cache_clear()
