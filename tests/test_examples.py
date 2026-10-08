"""The SaaS sample: what Get started and Simulate new activity rely on.

Get started offers every trigger in examples/saas/definitions.yaml, and the
only way a visitor sees one fire is Simulate new activity. A trigger the
simulation never feeds is a dead end: the visitor turns a rule on, presses
the button, and waits for a message that cannot come. So each one must get
a fresh row from examples/saas/activity.sql.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SAAS = REPO_ROOT / "examples" / "saas"
ADMIN_DSN = os.environ.get(
    "TEST_FIXTURE_ADMIN_URL",
    "postgresql://rowfire:rowfire@localhost:5433/rowfire_fixture",
)


def _reachable() -> bool:
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="fixture database unavailable")


@pytest.fixture
def saas():
    """The SaaS sample, seeded into a schema of its own and dropped after."""
    schema = f"saas_check_{uuid.uuid4().hex[:8]}"
    conn = psycopg.connect(ADMIN_DSN, autocommit=True)
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    for name in ("01_schema.sql", "02_seed.sql"):
        conn.execute((SAAS / "db" / name).read_bytes())
    try:
        yield conn
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _postgres_triggers() -> dict[str, dict]:
    """Every trigger the sample offers that reads the sample Postgres."""
    raw = yaml.safe_load((SAAS / "definitions.yaml").read_text())
    return {
        name: trigger
        for name, trigger in raw["triggers"].items()
        if trigger.get("source", "primary") == "primary"
    }


def _fresh(conn: psycopg.Connection, trigger: dict) -> int:
    """Rows the trigger matches whose clock is within the last minute."""
    clock = trigger["event_time"]
    row = conn.execute(
        f"SELECT count(*) FROM ({trigger['sql']}) AS t "
        f"WHERE t.{clock} BETWEEN now() - interval '1 minute' AND now() + interval '1 minute'"
    ).fetchone()
    return row[0]


def test_simulated_activity_feeds_every_trigger_get_started_offers(saas) -> None:
    triggers = _postgres_triggers()
    assert triggers, "the sample offers no Postgres triggers at all"

    saas.execute((SAAS / "activity.sql").read_bytes())

    starved = sorted(name for name, trigger in triggers.items() if _fresh(saas, trigger) == 0)
    assert starved == [], (
        f"Simulate new activity gives these triggers nothing to fire on: {starved}. "
        "Add rows for them to examples/saas/activity.sql."
    )
