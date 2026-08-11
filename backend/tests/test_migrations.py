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
            out[table] = (columns, indexes)
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
