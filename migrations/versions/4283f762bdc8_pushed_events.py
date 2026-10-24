"""pushed events

Events an app pushes with the in-app SDK, received by `rowfire ingest`: the
API keys it sends them with, the events themselves (deduplicated on the SDK's
event id), the latest catalog each app process reported, and the losses the
SDKs counted.

Revision ID: 4283f762bdc8
Revises: b7e2c4d9a1f3
Create Date: 2026-10-24 14:02:48.661358
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel.sql.sqltypes.*)
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4283f762bdc8"
down_revision: str | None = "b7e2c4d9a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_snapshot")),
        sa.UniqueConstraint("workspace_id", "instance_id", name="uq_catalog_snapshot_instance"),
    )
    op.create_table(
        "ingest_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key_prefix", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_key")),
        sa.UniqueConstraint("key_hash", name="uq_ingest_key_hash"),
    )
    op.create_index(
        op.f("ix_ingest_key_workspace_id"), "ingest_key", ["workspace_id"], unique=False
    )
    op.create_table(
        "ingested_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("subscription", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingested_event")),
        sa.UniqueConstraint("workspace_id", "event_id", name="uq_ingested_event_id"),
    )
    op.create_index(
        "ix_ingested_event_poll",
        "ingested_event",
        ["workspace_id", "subscription", "received_at"],
        unique=False,
    )
    op.create_index("ix_ingested_event_received", "ingested_event", ["received_at"], unique=False)
    op.create_table(
        "sdk_loss",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sdk_loss")),
        sa.UniqueConstraint("workspace_id", "reason", "day", name="uq_sdk_loss_day"),
    )


def downgrade() -> None:
    op.drop_table("sdk_loss")
    op.drop_index("ix_ingested_event_received", table_name="ingested_event")
    op.drop_index("ix_ingested_event_poll", table_name="ingested_event")
    op.drop_table("ingested_event")
    op.drop_index(op.f("ix_ingest_key_workspace_id"), table_name="ingest_key")
    op.drop_table("ingest_key")
    op.drop_table("catalog_snapshot")
