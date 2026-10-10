"""oauth grants

Tokens from a customer's OAuth consent -- Supabase's, for now -- encrypted the
same way a DSN is. A Supabase source points at one of these instead of
holding a database password.

Revision ID: b7e2c4d9a1f3
Revises: 926b40ca4d46
Create Date: 2026-10-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel.sql.sqltypes.*)
from alembic import op

revision: str = "b7e2c4d9a1f3"
down_revision: str | None = "926b40ca4d46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tokens_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("tokens_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("algorithm", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_grant")),
    )
    op.create_index(
        op.f("ix_oauth_grant_workspace_id"), "oauth_grant", ["workspace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_grant_workspace_id"), table_name="oauth_grant")
    op.drop_table("oauth_grant")
