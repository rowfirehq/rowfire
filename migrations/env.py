"""Alembic environment for the control-plane database.

The URL comes from ROWFIRE_PLATFORM_DSN rather than alembic.ini, so a
credential is never committed. Autogenerate compares against SQLModel's
metadata, which is only populated once the models module is imported.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from rowfire.platform import models  # noqa: F401  (registers the tables)
from rowfire.platform.db import resolve_dsn

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Compare only Rowfire's own tables.

    An extension (rowfire/extensions.py) may keep tables of its own in the
    same schema, under migrations of its own; autogenerate would otherwise
    offer to drop them. The cost: dropping one of Rowfire's own models is not
    detected, so that migration's DROP TABLE is written by hand.
    """
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or resolve_dsn()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catch a column whose type drifted from the model, not just
            # added and dropped columns.
            compare_type=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
