"""basic auth

HTTP Basic, which is what an API *token* means for Zendesk, Twilio, Stripe and
plenty of others. It needs two values, so the integration names a second
credential for the username half.

`auth_kind` is a Postgres enum, and autogenerate does not notice a new value in
one -- it saw the column and nothing else, which failed at the first insert
rather than at migration time. ALTER TYPE ... ADD VALUE also cannot be used in
the same transaction that adds it, hence the autocommit block.

Revision ID: 64f18bf0503e
Revises: fc38b58a2ab1
Create Date: 2026-10-06 20:26:46.069028
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel.sql.sqltypes.*)
from alembic import op

revision: str = "64f18bf0503e"
down_revision: str | None = "fc38b58a2ab1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE authkind ADD VALUE IF NOT EXISTS 'basic'")

    op.add_column(
        "integration",
        sa.Column(
            "auth_username_credential",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("integration", "auth_username_credential")
    # The enum value stays. Postgres cannot drop one without rewriting the
    # type and everything that references it, and an unused label is inert --
    # whereas a rewrite on the way *down* is exactly when you least want one.
