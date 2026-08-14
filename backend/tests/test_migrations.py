"""Migrations are exercised as a subprocess, the way a deploy runs them.

Importing Alembic in-process would share this interpreter's already-imported
`core.users`, and with it the module-level `engine` bound to whichever database
existed at import time. The subprocess gets a clean import and an explicit
RETINA_DB_PATH, which is the only way the round trip can be trusted.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.migration_helpers import BACKEND, ROLLBACK_AHEAD_SENTINEL, _alembic


def test_migrations_have_exactly_one_head(tmp_path):
    """Two branches that each add a revision off the same parent will merge
    without a textual conflict: different files, nothing overlapping, nothing a
    review would show. What they leave behind is an Alembic tree with two
    heads, and `alembic upgrade head` refusing to choose between them.

    That refusal reads "Multiple head revisions are present", which is not the
    wording the `elif` in deploy/start.sh tolerates, so it falls through to the
    refuse-to-boot `else` below and no environment starts until the heads are
    reconciled. The rest of this file goes red too, along with conftest.py's
    node schema template and every test behind it, but those all fail on
    `returncode == 0` with Alembic's reason left on the subprocess's stdout,
    where the assertion message does not carry it. This test sits first so it
    is the first failure of the run, and it prints the colliding revisions. The
    fix is to renumber the later one onto the other's head, or `alembic merge`.
    """
    # `heads` is answered from migrations/versions/ through the ScriptDirectory
    # alone and never imports env.py, so nothing is created at this path.
    # _alembic requires one all the same.
    result = _alembic("heads", db_path=tmp_path / "unused.db")
    # Both streams: Alembic's `util.err` puts a clean failure, a missing
    # alembic.ini say, on stdout behind a "FAILED:" prefix, and only an
    # uncaught traceback such as a dangling down_revision reaches stderr.
    # Reporting one of the two is how the failures described above lose their
    # reason.
    assert result.returncode == 0, result.stdout + result.stderr

    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, result.stdout


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


def _schema(db_path: Path) -> dict:
    """Structure of a SQLite file, normalised for comparison.

    Compared as sets rather than as `.schema` text: SQLAlchemy and Alembic emit
    the same structure with different whitespace and constraint ordering, and a
    text diff would fail on both without a single column being wrong.
    `alembic_version` is excluded because only one side of the comparison has it.
    """
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        tables = [
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        out = {}
        for table in tables:
            columns = {
                (name, kind.upper(), bool(notnull), default, bool(pk))
                for name, kind, notnull, default, pk in con.execute(
                    'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?)', (table,)
                )
            }
            indexes = set()
            for index_name, unique in con.execute('SELECT name, "unique" FROM pragma_index_list(?)', (table,)):
                if index_name.startswith("sqlite_autoindex"):
                    continue  # the implicit index behind a UNIQUE constraint
                index_columns = tuple(
                    column
                    for (column,) in con.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,))
                )
                indexes.add((index_name, bool(unique), index_columns))
            foreign_keys = {
                (referenced_table, from_col, to_col, on_update, on_delete)
                for referenced_table, from_col, to_col, on_update, on_delete in con.execute(
                    'SELECT "table", "from", "to", on_update, on_delete FROM pragma_foreign_key_list(?)', (table,)
                )
            }
            out[table] = (columns, indexes, foreign_keys)
        return out
    finally:
        con.close()


def _create_all(db_path: Path, *, with_nodes: bool = True) -> subprocess.CompletedProcess:
    """Build a database the pre-Alembic way, for comparison.

    with_nodes=False reproduces the droplets: core.nodes did not exist when
    their schema was built, so they carry the four auth tables and nothing else.
    """
    env = os.environ | {
        "RETINA_ENV": "test",
        "RETINA_DB_PATH": str(db_path),
        "RETINA_SCHEMA_SOURCE": "create_all",
    }
    nodes_import = "import core.nodes;  # noqa: F401  registers the tables\n" if with_nodes else ""
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            f"import asyncio; {nodes_import}"
            "from core.users import create_db_and_tables; "
            "asyncio.run(create_db_and_tables())",
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_migrations_produce_the_schema_create_all_produces(tmp_path):
    """Drift here passes the round trip and diverges on a fresh deploy."""
    migrated = tmp_path / "migrated.db"
    direct = tmp_path / "direct.db"

    up = _alembic("upgrade", "head", db_path=migrated)
    assert up.returncode == 0, up.stderr
    built = _create_all(direct)
    assert built.returncode == 0, built.stderr

    assert _schema(migrated) == _schema(direct)


def test_upgrading_a_create_all_database_succeeds(tmp_path):
    """The state of all three droplets: the four auth tables, no alembic_version,
    and no node tables, since core.nodes did not exist when they were built.

    Without the early return in 0001 this fails on `table user already exists`,
    and every deploy after the guard lands would refuse to boot.
    """
    db = tmp_path / "pre_existing.db"
    built = _create_all(db, with_nodes=False)
    assert built.returncode == 0, built.stderr

    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    stamped = _alembic("current", db_path=db)
    assert stamped.returncode == 0, stamped.stderr
    assert "head" in stamped.stdout, stamped.stdout + stamped.stderr


def test_rollback_ahead_sentinel_matches_alembics_wording(tmp_path):
    """deploy/start.sh greps a failed `alembic upgrade head`'s output for the
    literal substring ROLLBACK_AHEAD_SENTINEL to tell a tolerable rollback
    apart from a genuine migration failure (see the comment above that `elif`).
    Alembic controls the wording, not us, so this reproduces the actual
    scenario a rolled-back image sees: a database stamped ahead of the
    revisions its (older) migrations/versions/ directory knows about.

    A future Alembic release rewording the message fails this test loudly in
    CI, instead of start.sh's grep silently no longer matching in production.
    """
    db = tmp_path / "ahead.db"
    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    # An "older image": the same backend tree minus the newest revision. Only
    # core/ (env.py imports core.users and core.nodes to build target_metadata)
    # and migrations/ are needed; core/__init__.py does not import its sibling
    # modules eagerly, so copying the two files env.py touches is enough.
    old_image = tmp_path / "old_image"
    shutil.copytree(BACKEND / "core", old_image / "core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(BACKEND / "migrations", old_image / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    # The newest revision, found rather than named: revisions are numbered
    # 000N_*.py so the last by filename is the head. Naming one here meant that
    # adding a revision on top of it left the older one's removal orphaning every
    # revision after it, and Alembic then raised a bare KeyError on the missing
    # down_revision instead of the message this test exists to pin.
    versions = sorted((old_image / "migrations" / "versions").glob("[0-9]*.py"))
    versions[-1].unlink()
    shutil.copyfile(BACKEND / "alembic.ini", old_image / "alembic.ini")

    env = os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db)}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=old_image,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert ROLLBACK_AHEAD_SENTINEL in combined, combined
