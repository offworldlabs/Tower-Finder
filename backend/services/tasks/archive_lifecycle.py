"""Archive lifecycle management: offload old detections to R2, prune local disk.

Runs every hour as a background task. The lifecycle is:
  1. Files older than OFFLOAD_AGE_DAYS → upload to R2 under "archive/" prefix
  2. Files older than RETENTION_DAYS   → delete from local disk
                                         (skipped when RETENTION_DAYS <= 0)
  3. Empty date directories are cleaned up

R2 retains uploaded files indefinitely. The default config keeps local files
forever (RETENTION_DAYS = 0); set a positive value if disk pressure becomes
an issue on a given deployment.
"""

import logging
import os
import time
from pathlib import Path

from config.constants import (
    ARCHIVE_OFFLOAD_AGE_DAYS,
    ARCHIVE_RETENTION_DAYS,
)

logger = logging.getLogger(__name__)

_ARCHIVE_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))) / "coverage_data" / "archive"

# Caps to prevent runaway work in a single cycle
_MAX_UPLOAD_PER_CYCLE = 500
_MAX_DELETE_PER_CYCLE = 2000


def run_archive_lifecycle() -> dict:
    """Synchronous — run in a thread executor.

    Returns summary dict: {uploaded, deleted, errors, skipped}.
    """
    from services.r2_client import is_enabled as r2_enabled
    from services.r2_client import upload_file as r2_upload

    stats = {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0}
    now = time.time()
    offload_cutoff = now - (ARCHIVE_OFFLOAD_AGE_DAYS * 86400)
    # ARCHIVE_RETENTION_DAYS <= 0 disables local deletion (R2 keeps forever).
    deletion_enabled = ARCHIVE_RETENTION_DAYS > 0
    delete_cutoff = now - (ARCHIVE_RETENTION_DAYS * 86400) if deletion_enabled else 0.0

    if not _ARCHIVE_DIR.exists():
        return stats

    use_r2 = r2_enabled()

    for json_file in _iter_archive_files():
        if stats["uploaded"] + stats["deleted"] >= _MAX_DELETE_PER_CYCLE:
            stats["skipped"] += 1
            continue

        try:
            mtime = json_file.stat().st_mtime
        except OSError:
            continue

        rel = json_file.relative_to(_ARCHIVE_DIR)
        r2_key = f"archive/{rel}"

        # Phase 1: Upload to R2 if old enough and not yet uploaded
        if use_r2 and mtime < offload_cutoff and stats["uploaded"] < _MAX_UPLOAD_PER_CYCLE:
            if r2_upload(r2_key, str(json_file)):
                stats["uploaded"] += 1
            else:
                stats["errors"] += 1

        # Phase 2: Delete from local disk if past retention (only when enabled)
        if deletion_enabled and mtime < delete_cutoff:
            try:
                json_file.unlink()
                stats["deleted"] += 1
            except OSError:
                stats["errors"] += 1

    # Phase 3: Clean up empty directories (bottom-up)
    _prune_empty_dirs(_ARCHIVE_DIR)

    if stats["uploaded"] or stats["deleted"]:
        logger.info(
            "Archive lifecycle: uploaded=%d deleted=%d errors=%d skipped=%d",
            stats["uploaded"], stats["deleted"], stats["errors"], stats["skipped"],
        )

    return stats


def _iter_archive_files():
    """Yield .json files from the archive directory, oldest first."""
    if not _ARCHIVE_DIR.exists():
        return
    # Walk year/month/day/node dirs in order — naturally chronological
    try:
        for year_dir in sorted(_ARCHIVE_DIR.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir():
                        continue
                    for node_dir in sorted(day_dir.iterdir()):
                        if not node_dir.is_dir():
                            continue
                        for f in sorted(node_dir.glob("*.json")):
                            yield f
    except OSError:
        logger.debug("Error iterating archive directory", exc_info=True)


def _prune_empty_dirs(base: Path):
    """Remove empty leaf directories bottom-up."""
    try:
        for dirpath, dirnames, filenames in os.walk(str(base), topdown=False):
            if dirpath == str(base):
                continue
            if not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    except OSError:
        pass
