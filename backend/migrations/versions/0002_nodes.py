"""Nodes, their configuration versions and their tokens.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Three new tables and nothing else. Code from before this revision has no
# queries that name them.
rollback_safety = "additive"


def upgrade() -> None:
    # Unlike 0001, this creates unconditionally rather than inspecting for a
    # pre-existing table. 0001's reconciliation exists only because the three
    # droplets were built by create_all before Alembic existed; the node
    # tables have never been created any other way than by this revision, so
    # no equivalent pre-Alembic state can exist for them to reconcile with. A
    # guard was tried here earlier in this branch and removed after review: it
    # turned a loud, correct "table nodes already exists" failure into a
    # silent skip, and it only inspected one of the three tables below.
    op.create_table(
        "nodes",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("node_ref", sa.String(length=15), nullable=False),
        sa.Column("board_model", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("active_config_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("licence_version", sa.String(length=32), nullable=True),
        sa.Column("licence_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_management_version", sa.String(length=32), nullable=True),
        sa.Column("remote_management_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publication", sa.String(length=8), server_default="public", nullable=False),
        sa.Column("publication_version", sa.String(length=32), nullable=True),
        sa.Column("publication_chosen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index("ix_nodes_node_ref", "nodes", ["node_ref"], unique=True)

    op.create_table(
        "node_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rx_lat", sa.Float(), nullable=False),
        sa.Column("rx_lon", sa.Float(), nullable=False),
        sa.Column("rx_alt_ft", sa.Float(), nullable=False),
        sa.Column("tx_lat", sa.Float(), nullable=False),
        sa.Column("tx_lon", sa.Float(), nullable=False),
        sa.Column("tx_alt_ft", sa.Float(), nullable=False),
        sa.Column("tx_callsign", sa.String(length=32), nullable=False),
        sa.Column("fc_hz", sa.Float(), nullable=False),
        sa.Column("fs_hz", sa.Float(), nullable=False),
        sa.Column("beam_width_deg", sa.Float(), nullable=False),
        # Nullable, and it matters: null is broadside, 0.0 is aimed due north.
        sa.Column("beam_azimuth_deg", sa.Float(), nullable=True),
        sa.Column("max_range_km", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "version", name="uq_node_configs_node_version"),
    )
    op.create_index("ix_node_configs_node_id", "node_configs", ["node_id"])

    op.create_table(
        "node_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_node_tokens_node_id", "node_tokens", ["node_id"])
    op.create_index("ix_node_tokens_token_hash", "node_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_node_tokens_token_hash", table_name="node_tokens")
    op.drop_index("ix_node_tokens_node_id", table_name="node_tokens")
    op.drop_table("node_tokens")
    op.drop_index("ix_node_configs_node_id", table_name="node_configs")
    op.drop_table("node_configs")
    op.drop_index("ix_nodes_node_ref", table_name="nodes")
    op.drop_table("nodes")
