"""declared action parameters

An action now says what it needs -- name, type, label, required -- instead of
having it inferred from the placeholders in its body. Nullable and empty for
existing rows: `action_parameters` falls back to inference when the column is
empty, so nothing needs backfilling and no action stops working.

Revision ID: 9c6490af597b
Revises: d3aaddf03507
Create Date: 2026-10-06 17:56:21.422104
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel.sql.sqltypes.*)
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c6490af597b"
down_revision: str | None = "d3aaddf03507"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action",
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("action", "parameters")
