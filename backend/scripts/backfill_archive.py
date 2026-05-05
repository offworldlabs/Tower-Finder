"""One-shot backfill: convert legacy JSON detection archives in R2 to Parquet.

Usage:
    PYTHONPATH=. python3 scripts/backfill_archive.py [--prefix archive/]
                                                     [--limit N]
                                                     [--dry-run]
                                                     [--force]

Behaviour:
- Lists all keys under --prefix (default ``archive/``) ending in ``.json``.
- For each, downloads, converts via backfill.json_to_parquet, uploads to the
  Hive-partitioned target key (also under ``archive/``).
- Skips keys whose target already exists in R2 unless --force is given.
- The original JSON is **not** deleted; that decision is left for a separate
  cleanup pass after the new files are verified.
"""

from __future__ import annotations

import argparse
import logging
import sys

from backfill.json_to_parquet import convert_legacy_bytes
from services import r2_client

logger = logging.getLogger("backfill")


def _target_exists(key: str) -> bool:
    """Check whether the target Parquet key already lives in R2."""
    return r2_client.download_bytes(key) is not None


def run(prefix: str = "archive/", limit: int | None = None,
        dry_run: bool = False, force: bool = False) -> dict:
    if not r2_client.is_enabled():
        logger.error("R2 is not configured; aborting.")
        return {"error": "r2_disabled"}

    stats = {"scanned": 0, "converted": 0, "skipped": 0, "errors": 0}

    keys = [k for k in r2_client.list_keys(prefix) if k.endswith(".json")]
    if limit:
        keys = keys[:limit]
    logger.info("Found %d legacy JSON keys under %s", len(keys), prefix)

    for src_key in keys:
        stats["scanned"] += 1
        try:
            raw = r2_client.download_bytes(src_key)
            if not raw:
                stats["errors"] += 1
                continue
            target_key, parquet_bytes = convert_legacy_bytes(raw)
            if not force and _target_exists(target_key):
                stats["skipped"] += 1
                continue
            if dry_run:
                logger.info("DRY: %s -> %s (%d bytes)", src_key, target_key, len(parquet_bytes))
            else:
                ok = r2_client.upload_bytes(
                    target_key, parquet_bytes,
                    content_type="application/octet-stream",
                )
                if not ok:
                    stats["errors"] += 1
                    continue
            stats["converted"] += 1
            if stats["scanned"] % 100 == 0:
                logger.info("Progress: %s", stats)
        except Exception:
            logger.exception("Failed to convert %s", src_key)
            stats["errors"] += 1

    logger.info("Backfill done: %s", stats)
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="archive/")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    stats = run(prefix=args.prefix, limit=args.limit,
                dry_run=args.dry_run, force=args.force)
    if stats.get("error"):
        sys.exit(2)


if __name__ == "__main__":
    main()
