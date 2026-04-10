#!/usr/bin/env python3
"""Nightly backup of critical data to Backblaze B2.

Intended to run via cron or systemd timer:

    0 3 * * * cd /opt/tower-finder && python3 backend/scripts/backup_to_b2.py

Requires env vars: B2_KEY_ID, B2_APP_KEY, B2_BUCKET_NAME
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backup")

BACKEND_DIR = Path(__file__).resolve().parent.parent
PATHS_TO_BACKUP = [
    BACKEND_DIR / "coverage_data" / "archive",
    BACKEND_DIR / "data" / "users.json",
    BACKEND_DIR / "data" / "events.json",
]


def main():
    key_id = os.getenv("B2_KEY_ID", "")
    app_key = os.getenv("B2_APP_KEY", "")
    bucket_name = os.getenv("B2_BUCKET_NAME", "")

    if not all([key_id, app_key, bucket_name]):
        logger.error("B2_KEY_ID, B2_APP_KEY, B2_BUCKET_NAME must all be set")
        sys.exit(1)

    from b2sdk.v2 import B2Api, InMemoryAccountInfo

    info = InMemoryAccountInfo()
    api = B2Api(info)
    api.authorize_account("production", key_id, app_key)
    bucket = api.get_bucket_by_name(bucket_name)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uploaded = 0

    for path in PATHS_TO_BACKUP:
        if not path.exists():
            logger.warning("Skip (missing): %s", path)
            continue

        if path.is_file():
            remote = f"backups/{date_tag}/{path.name}"
            logger.info("Uploading %s → %s", path, remote)
            bucket.upload_local_file(str(path), remote)
            uploaded += 1
        elif path.is_dir():
            for fpath in sorted(path.rglob("*")):
                if not fpath.is_file():
                    continue
                rel = fpath.relative_to(BACKEND_DIR)
                remote = f"backups/{date_tag}/{rel}"
                logger.info("Uploading %s → %s", fpath, remote)
                bucket.upload_local_file(str(fpath), remote)
                uploaded += 1

    logger.info("Backup complete: %d files uploaded to %s", uploaded, bucket_name)


if __name__ == "__main__":
    main()
