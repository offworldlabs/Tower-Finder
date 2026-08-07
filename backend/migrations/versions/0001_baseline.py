"""Baseline: the four existing auth tables.

Revision ID: 0001
Revises:

Phase 1 removes or rebuilds three of these four. The baseline creates them
anyway, because a revision chain has to start from what the databases actually
hold: a later revision cannot drop `invites` unless something upstream created
it, and all three droplets carry all four tables today.
"""

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy.generics import GUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("user"):
        # A database built by create_all before Alembic existed. Its tables are
        # already correct, so recording the revision is the whole job and
        # creating them again would fail. This is the state of all three
        # droplets, and it is what lets them adopt migrations on a plain deploy
        # rather than needing `alembic stamp` run by hand on each.
        return

    op.create_table(
        "user",
        sa.Column("name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("avatar", sa.String(length=512), server_default="", nullable=False),
        sa.Column("provider", sa.String(length=50), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", GUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_email", "user", ["email"], unique=True)

    op.create_table(
        "invites",
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_invites_email", "invites", ["email"])

    op.create_table(
        "node_owners",
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index("ix_node_owners_user_id", "node_owners", ["user_id"])

    op.create_table(
        "claim_codes",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("used_by_node_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index("ix_claim_codes_user_id", "claim_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_claim_codes_user_id", table_name="claim_codes")
    op.drop_table("claim_codes")
    op.drop_index("ix_node_owners_user_id", table_name="node_owners")
    op.drop_table("node_owners")
    op.drop_index("ix_invites_email", table_name="invites")
    op.drop_table("invites")
    op.drop_index("ix_user_email", table_name="user")
    op.drop_table("user")
