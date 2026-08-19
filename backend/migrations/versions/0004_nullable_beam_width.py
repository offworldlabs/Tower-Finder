"""beam_width_deg becomes nullable on node_configs.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Widening only: code predating this revision still supplies a width on every
# insert and still reads the column it always read. The downgrade below is the
# part that can fail, and that is a separate question from whether the older
# code can serve against this schema.
rollback_safety = "additive"


def upgrade() -> None:
    # batch_alter_table rather than a plain alter_column: SQLite has no ALTER
    # COLUMN, so Alembic copies the table into a new one with the corrected
    # definition, moves the rows and swaps it in, carrying the indexes and
    # constraints across. 0002 created the column NOT NULL and is deployed on all
    # three droplets, so it cannot be corrected where it was written.
    with op.batch_alter_table("node_configs") as batch_op:
        batch_op.alter_column("beam_width_deg", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # This fails, loudly and part-way, if any row has a null width by the time it
    # runs, which after contract 1.1.1 is every row a real node wrote: the table
    # copy hits the NOT NULL and leaves the database stamped at 0004. That is the
    # honest outcome. The alternative is backfilling a width onto nodes that never
    # reported one, and an invented antenna geometry is worse than a failed
    # rollback because nothing downstream can tell it from a measurement.
    with op.batch_alter_table("node_configs") as batch_op:
        batch_op.alter_column("beam_width_deg", existing_type=sa.Float(), nullable=False)
