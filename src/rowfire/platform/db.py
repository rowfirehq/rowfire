"""Engine and session handling for the control-plane database.

Note what is *not* here: `default_transaction_read_only`. That guard belongs on
connections to the customer's database (engine.py) and would be actively wrong
here -- this is the one database the platform is supposed to write to. Keeping
the two connection paths in separate modules is deliberate, so a change to one
cannot silently relax the other.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlmodel import Session, SQLModel

PLATFORM_DSN_ENV = "ROWFIRE_PLATFORM_DSN"

_engine: Engine | None = None


class PlatformError(Exception):
    """Raised for control-plane connection problems. Never carries a DSN."""


def resolve_dsn(env_var: str = PLATFORM_DSN_ENV) -> str:
    dsn = os.environ.get(env_var)
    if not dsn:
        raise PlatformError(
            f"{env_var} is not set. Point it at the control-plane Postgres, e.g.\n"
            f"  export {env_var}="
            f"'postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform'"
        )
    return dsn


def get_engine(dsn: str | None = None, echo: bool = False) -> Engine:
    """Process-wide engine. Built once; psycopg pools underneath."""
    global _engine
    if _engine is None or dsn is not None:
        target = dsn or resolve_dsn()
        _engine = create_engine(
            target,
            echo=echo,
            pool_pre_ping=True,  # a scheduler outlives network blips
            future=True,
        )
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. For tests, and for reconfiguration."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A transactional session. Commits on success, rolls back on error."""
    with Session(engine or get_engine()) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def check_ready(engine: Engine | None = None) -> None:
    """Confirm the control plane is reachable and migrated.

    Separate from `get_engine` because an engine is lazy -- it connects on
    first use, which would be somewhere deep inside a request. The control
    plane is now the only store, so a process that depends on it should find
    out at startup, not when the first page half-loads.

    A missing table is reported as "not migrated" rather than as a raw
    ProgrammingError, because that is the actual remedy.
    """
    from sqlalchemy import text

    try:
        resolved = engine or get_engine()
    except PlatformError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"could not build a control-plane engine: {exc}") from exc

    try:
        with resolved.connect() as conn:
            conn.execute(text("SELECT 1 FROM definition_version LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip().splitlines()[0][:200]
        if "does not exist" in detail:
            raise PlatformError(
                f"the control-plane schema is not migrated ({detail}). Run `alembic upgrade head`."
            ) from exc
        raise PlatformError(f"control plane unreachable: {detail}") from exc


def create_all(engine: Engine | None = None) -> None:
    """Create the schema directly from the models.

    For tests and local scratch databases only. Real deployments go through
    Alembic, so that a schema change is reviewable and reversible.
    """
    from . import models  # noqa: F401  (import registers the tables)

    SQLModel.metadata.create_all(engine or get_engine())
