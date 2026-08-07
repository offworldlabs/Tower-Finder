"""Migrations are exercised as a subprocess, the way a deploy runs them.

Importing Alembic in-process would share this interpreter's already-imported
`core.users`, and with it the module-level `engine` bound to whichever database
existed at import time. The subprocess gets a clean import and an explicit
RETINA_DB_PATH, which is the only way the round trip can be trusted.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


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


def test_upgrade_downgrade_upgrade_round_trips(tmp_path):
    """The downgrade is run rather than assumed.

    A downgrade nobody has executed is a downgrade that does not work, and the
    cost of finding that out rises with every revision added after it.
    """
    db = tmp_path / "round_trip.db"

    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base", db_path=db)
    assert down.returncode == 0, down.stderr

    again = _alembic("upgrade", "head", db_path=db)
    assert again.returncode == 0, again.stderr
