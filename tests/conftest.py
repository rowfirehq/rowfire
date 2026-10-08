from __future__ import annotations

import os
from pathlib import Path

import pytest

from rowfire.definitions import (
    DedupPolicy,
    Definitions,
    Rule,
    Trigger,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONTROL_PLANE = "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform"


def _test_database_dsn(base: str) -> str:
    """Point tests at their own database, never the one the app is using.

    The control-plane tests TRUNCATE and lease trigger rows. Sharing a database
    with a running worker means the worker leases a trigger a test expected to
    get, and TRUNCATE blocks behind the worker's transactions -- so the suite
    fails or hangs depending on timing, with nothing to suggest that a
    container is the reason. Owning a separate database removes the class of
    problem instead of documenting a workaround.
    """
    head, _, name = base.rpartition("/")
    if name.endswith("_test"):
        return base
    return f"{head}/{name}_test"


def pytest_configure(config: pytest.Config) -> None:
    """Create and migrate the test control plane before anything is collected.

    Runs here rather than in a fixture because the test modules read
    ROWFIRE_PLATFORM_DSN at import time, which happens during collection.
    """
    import sqlalchemy
    from sqlalchemy import text

    target = _test_database_dsn(os.environ.get("ROWFIRE_PLATFORM_DSN", DEFAULT_CONTROL_PLANE))
    head, _, name = target.rpartition("/")

    try:
        admin = sqlalchemy.create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
    except Exception:
        # No control plane available: the db-marked tests skip themselves.
        return

    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", target)
    command.upgrade(alembic_config, "head")

    os.environ["ROWFIRE_PLATFORM_DSN"] = target


@pytest.fixture
def completed_orders() -> Trigger:
    """The canonical trigger: one row per completed, paid, non-test order.

    A plain SELECT rather than a predicate on a modelled entity -- which is the
    whole point of format 2. The columns the rules key on are columns this
    query returns, and nothing checks them against a schema model.
    """
    return Trigger(
        description="An order finished and was paid for",
        sql=(
            "SELECT id, customer_id, completed_at, total_amount "
            "FROM orders "
            "WHERE status = 4 AND paid_at IS NOT NULL AND is_test = false"
        ),
        event_time="completed_at",
        key=["id"],
    )


@pytest.fixture
def customer_ordered() -> Trigger:
    """The same tables at a different grain: one row per customer.

    Joined columns come along for free, which is why enrichment is not a
    separate feature. The key is `customer_id`, so rules on this trigger dedup
    per customer rather than per order.
    """
    return Trigger(
        description="A customer completed an order",
        sql=(
            "SELECT c.id AS customer_id, c.first_name, c.phone, "
            "o.id AS order_id, o.total_amount, o.completed_at "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.status IN (4, 7) AND o.is_test = false"
        ),
        event_time="completed_at",
        key=["customer_id"],
    )


@pytest.fixture
def definitions(completed_orders, customer_ordered) -> Definitions:
    """Two triggers, and two rules sharing one of them.

    Two rules on one trigger rather than one each, because almost every
    interesting property of the system -- fan-out, independent promotion, a
    ledger keyed on the rule -- is invisible with one rule per trigger. They
    sit on `customer_ordered` specifically: a weekly cap is only meaningful at
    a grain that repeats, and `completed_orders` is keyed on the order id.
    """
    return Definitions(
        triggers={
            "completed_orders": completed_orders,
            "customer_ordered": customer_ordered,
        },
        rules={
            "tell_ops": Rule(
                trigger="completed_orders",
                description="Every completed order, once",
                policy=DedupPolicy.once_ever,
            ),
            "thank_the_customer": Rule(
                trigger="customer_ordered",
                description="At most one thank-you per customer per week",
                policy=DedupPolicy.once_per_period,
                period="week",
            ),
            "nudge_ops": Rule(
                trigger="customer_ordered",
                description="Same trigger, daily instead of weekly",
                policy=DedupPolicy.once_per_period,
                period="day",
            ),
        },
    )
