"""integrations and actions

Connectors become integrations; connector actions become actions.

Written by hand rather than from autogenerate, which proposed dropping both
tables and creating new ones. That would have discarded every configured
credential and every rule binding -- a rename is the same end state with none
of the loss, and it is reversible, which a drop is not.

The credential column changes meaning as well as name: it held one encrypted
secret and now holds an encrypted JSON object of them. Existing rows are left
exactly as they are; `reveal_credentials` reads a bare string as the single
credential the auth config names, so no re-encryption is needed and the master
key is not required to run this migration.

Revision ID: d3aaddf03507
Revises: 68b309b0fac4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3aaddf03507"
down_revision: str | None = "68b309b0fac4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- connector -> integration
    op.rename_table("connector", "integration")
    op.alter_column("integration", "spec", new_column_name="provider")
    op.alter_column("integration", "secret_ciphertext", new_column_name="credentials_ciphertext")
    op.alter_column("integration", "secret_nonce", new_column_name="credentials_nonce")

    op.add_column("integration", sa.Column("description", sa.Text(), nullable=True))
    # server_default on the way in so existing rows get a value, then dropped
    # so the application stays the only thing deciding defaults.
    op.add_column(
        "integration",
        sa.Column("auth_credential", sa.String(), nullable=False, server_default="token"),
    )
    op.add_column(
        "integration",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "integration",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("integration", "auth_credential", server_default=None)
    op.alter_column("integration", "enabled", server_default=None)
    op.alter_column("integration", "updated_at", server_default=None)

    op.execute("ALTER TABLE integration RENAME CONSTRAINT pk_connector TO pk_integration")
    op.execute("ALTER TABLE integration RENAME CONSTRAINT uq_connector_name TO uq_integration_name")
    op.execute("ALTER INDEX ix_connector_workspace_id RENAME TO ix_integration_workspace_id")

    # --- connector_action -> action
    op.rename_table("connector_action", "action")
    op.alter_column("action", "connector_id", new_column_name="integration_id")
    op.add_column(
        "action",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "action",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("action", "created_at", server_default=None)
    op.alter_column("action", "updated_at", server_default=None)

    op.execute("ALTER TABLE action RENAME CONSTRAINT pk_connector_action TO pk_action")
    op.execute("ALTER TABLE action RENAME CONSTRAINT uq_connector_action_name TO uq_action_name")
    op.execute(
        "ALTER TABLE action RENAME CONSTRAINT fk_connector_action_connector "
        "TO fk_action_integration"
    )

    # --- the two tables that point at an action
    op.alter_column("rule_binding", "connector_action_id", new_column_name="action_id")
    op.execute(
        "ALTER TABLE rule_binding RENAME CONSTRAINT fk_rule_binding_connector_action_id "
        "TO fk_rule_binding_action_id"
    )

    op.alter_column("delivery", "connector_action_id", new_column_name="action_id")
    op.execute(
        "ALTER TABLE delivery RENAME CONSTRAINT fk_delivery_connector_action TO fk_delivery_action"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE delivery RENAME CONSTRAINT fk_delivery_action TO fk_delivery_connector_action"
    )
    op.alter_column("delivery", "action_id", new_column_name="connector_action_id")

    op.execute(
        "ALTER TABLE rule_binding RENAME CONSTRAINT fk_rule_binding_action_id "
        "TO fk_rule_binding_connector_action_id"
    )
    op.alter_column("rule_binding", "action_id", new_column_name="connector_action_id")

    op.execute(
        "ALTER TABLE action RENAME CONSTRAINT fk_action_integration "
        "TO fk_connector_action_connector"
    )
    op.execute("ALTER TABLE action RENAME CONSTRAINT uq_action_name TO uq_connector_action_name")
    op.execute("ALTER TABLE action RENAME CONSTRAINT pk_action TO pk_connector_action")
    op.drop_column("action", "updated_at")
    op.drop_column("action", "created_at")
    op.alter_column("action", "integration_id", new_column_name="connector_id")
    op.rename_table("action", "connector_action")

    op.execute("ALTER INDEX ix_integration_workspace_id RENAME TO ix_connector_workspace_id")
    op.execute("ALTER TABLE integration RENAME CONSTRAINT uq_integration_name TO uq_connector_name")
    op.execute("ALTER TABLE integration RENAME CONSTRAINT pk_integration TO pk_connector")
    op.drop_column("integration", "updated_at")
    op.drop_column("integration", "enabled")
    op.drop_column("integration", "auth_credential")
    op.drop_column("integration", "description")
    op.alter_column("integration", "credentials_nonce", new_column_name="secret_nonce")
    op.alter_column("integration", "credentials_ciphertext", new_column_name="secret_ciphertext")
    op.alter_column("integration", "provider", new_column_name="spec")
    op.rename_table("integration", "connector")
