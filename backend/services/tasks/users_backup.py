"""Daily SQLite backup of users.db to R2 with N-day retention.

Why this exists: users.db holds all auth state — users, invites, claim codes,
node ownership. Losing it means every user has to be re-onboarded by hand,
and any in-flight invites/claim codes vanish. The drive can fail, the
container can be deleted, the WAL/SHM files can desync — none of those are
recoverable from the runtime alone, so the only safe answer is an off-host
copy.

Implementation choices worth flagging:
  - `VACUUM INTO` is the safest snapshot mechanism. It's an online op (no
    locks held), produces a self-consistent file (no -wal/-shm needed), and
    is atomic from the reader's perspective. Plain `cp` of users.db can
    capture a half-committed write if a transaction is in flight.
  - We upload to R2 under `backups/users-db/YYYY-MM-DD.db`. One key per day
    means re-running the task on the same day is idempotent (overwrites,
    same key) and the prefix lists chronologically.
  - Retention is enforced after the upload — we list all backup keys, sort
    by date in the key, and delete anything older than _RETENTION_DAYS. R2
    is the source of truth for what counts as "still retained".
"""

import logging
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from config.constants import USERS_DB_BACKUP_INTERVAL_S, USERS_DB_BACKUP_RETENTION_DAYS

logger = logging.getLogger(__name__)

_BACKUP_PREFIX = "backups/users-db/"
_KEY_DATE_RE = re.compile(r"backups/users-db/(\d{4}-\d{2}-\d{2})\.db$")


def _users_db_path() -> Path:
    """Resolve users.db on the filesystem.

    Imported lazily so test suites that don't touch the auth DB don't pay
    the SQLAlchemy import cost up-front.
    """
    from core.users import DATABASE_URL

    # DATABASE_URL is "sqlite+aiosqlite:///<absolute path>"
    return Path(DATABASE_URL.split("///", 1)[1])


def _today_key() -> str:
    return f"{_BACKUP_PREFIX}{datetime.now(timezone.utc).date().isoformat()}.db"


def run_users_db_backup() -> dict:
    """Synchronous — designed to be run inside a thread executor.

    Returns {"uploaded": str|None, "deleted": int, "skipped": str|None}.
    """
    from services.r2_client import delete_keys, is_enabled, list_keys, upload_file

    stats: dict = {"uploaded": None, "deleted": 0, "skipped": None}

    if not is_enabled():
        stats["skipped"] = "r2-disabled"
        logger.warning("users_backup: R2 disabled — skipping (data has no off-host copy)")
        return stats

    db_path = _users_db_path()
    if not db_path.exists():
        stats["skipped"] = "db-missing"
        return stats

    # Atomic snapshot via VACUUM INTO — safer than cp, never observes a
    # half-committed write even with WAL active. The temp file is created
    # alongside users.db so the rename stays on the same filesystem.
    with tempfile.NamedTemporaryFile(
        prefix="users-backup-", suffix=".db",
        dir=db_path.parent, delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"VACUUM INTO '{tmp_path}'")
        finally:
            conn.close()

        key = _today_key()
        if not upload_file(key, str(tmp_path), content_type="application/x-sqlite3"):
            stats["skipped"] = "upload-failed"
            return stats
        stats["uploaded"] = key
        logger.info("users_backup: uploaded %s (%d bytes)", key, tmp_path.stat().st_size)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Retention: drop any backup older than RETENTION_DAYS based on the date
    # encoded in the key (not R2's LastModified, which would surprise anyone
    # who manually re-uploads an old snapshot).
    cutoff = datetime.now(timezone.utc).date()
    cutoff_ts = time.mktime(cutoff.timetuple()) - USERS_DB_BACKUP_RETENTION_DAYS * 86400
    expired: list[str] = []
    for k in list_keys(_BACKUP_PREFIX):
        m = _KEY_DATE_RE.match(k)
        if not m:
            continue  # unknown shape — leave it alone
        try:
            key_date = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if key_date.timestamp() < cutoff_ts:
            expired.append(k)

    if expired:
        stats["deleted"] = delete_keys(expired)
        logger.info("users_backup: pruned %d expired backups", stats["deleted"])

    return stats


async def users_backup_task():
    """Async wrapper — runs the backup once per USERS_DB_BACKUP_INTERVAL_S.

    Uses an explicit run-on-startup-after-delay rather than running
    immediately on boot, because main.py's lifespan already restores state
    and we don't want backup churn during a deploy.
    """
    import asyncio

    from core import state

    while True:
        await asyncio.sleep(USERS_DB_BACKUP_INTERVAL_S)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_users_db_backup)
            state.task_last_success["users_db_backup"] = time.time()
        except Exception:
            state.task_error_counts["users_db_backup"] += 1
            logger.exception("users_db backup failed")
