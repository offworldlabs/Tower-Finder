"""Report anything the models expect that the database does not have.

`alembic upgrade head` fails the same way whether the revision recorded in the
database is a descendant of head (a rollback, or an older branch: the database
carries more than this tree needs, and is safe to run against) or a sibling from
a divergent branch (this tree's own migrations were never applied, and running
against it is not safe). Alembic cannot tell the two apart, because in both
cases the recorded revision is simply absent from this tree's revision graph.
The schema itself can: a rollback leaves a superset of what the models want, a
sibling leaves a gap.

Tables and columns only, since that is what "no such table" and "no such column"
are made of, and extra tables or columns are exactly what the safe case looks
like, so they are ignored. Exits 0 when the database satisfies the models, 1
when it does not, listing what is missing.

Used by the justfile's `migrate` recipe to decide whether an unlocatable
revision is tolerable. Run it by hand the same way:

    cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.check_schema
"""

import asyncio
import sys

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from core import nodes  # noqa: F401  registers the node tables on Base.metadata
from core.users import DATABASE_URL, Base


def _missing(connection) -> list[str]:
    inspector = inspect(connection)
    present = set(inspector.get_table_names())
    gaps = []
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            gaps.append(f"table {table.name}")
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        gaps.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
    return gaps


async def main() -> int:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            gaps = await connection.run_sync(_missing)
    finally:
        await engine.dispose()
    if not gaps:
        return 0
    # Name the database rather than leaving the caller to assume the default:
    # RETINA_DB_PATH may well have pointed this at a scratch file instead.
    print(f"  {DATABASE_URL} is missing:")
    for gap in gaps:
        print(f"    {gap}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
