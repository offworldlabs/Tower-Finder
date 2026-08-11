"""Shared Alembic-subprocess helper.

conftest.py's `node_session` fixture and test_migrations.py both need to run
`alembic upgrade head` in a subprocess against a scratch database (see
test_migrations.py's module docstring for why it has to be a subprocess
rather than an in-process call). A small module under tests/ is the only
place both can import from without a circular import: conftest.py is
imported by pytest before any test module, so it cannot import from
test_migrations.py.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# The literal substring deploy/start.sh greps for in a failed `alembic upgrade
# head`'s output to tell a tolerable rollback (the database is ahead of the
# revisions this image's migrations/versions/ directory knows about) from a
# genuine migration failure. Alembic controls this wording, not us, so it is
# defined once here rather than typed out separately in the shell and in the
# test: test_migrations.py's test_rollback_ahead_sentinel_matches_alembics_wording
# asserts it still appears in Alembic's real output, and deploy/start.sh's
# `elif` comments point back here. If a future Alembic release rewords the
# message, that test fails loudly in CI instead of start.sh's grep silently
# no longer matching in production.
ROLLBACK_AHEAD_SENTINEL = "Can't locate revision"


def _alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess:
    env = os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db_path)}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
