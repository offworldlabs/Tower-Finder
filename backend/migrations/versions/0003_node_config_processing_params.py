"""cpi_s and the two tolerances on node_configs.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Destructive despite only adding columns: all three are NOT NULL with no
# server_default, so an insert from code predating them supplies no value and
# the constraint rejects the row. Nothing writes node_configs today, which is
# what makes the upgrade safe to run unattended (see the comment below), but
# the grading describes the schema contract rather than the current traffic,
# and phase 1's config routes are what will write it.
rollback_safety = "destructive"

# Required on the wire since contract 1.1.0, so not null like the other twelve.
# There is deliberately no server_default, and it is what makes this revision safe
# to run unattended: SQLite accepts ADD COLUMN ... NOT NULL only while the table is
# empty, and node_configs is empty because no route writes it yet. If that has
# stopped being true by the time this runs, it fails with "Cannot add a NOT NULL
# column with default value NULL" and leaves the database stamped at 0002, rather
# than backfilling a CPI onto rows whose nodes never reported one. Recovering from
# that means making the columns nullable, not choosing a default here.
_COLUMNS = ("cpi_s", "delay_tolerance_us", "doppler_tolerance_hz")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("node_configs", sa.Column(column, sa.Float(), nullable=False))


def downgrade() -> None:
    for column in _COLUMNS:
        op.drop_column("node_configs", column)
