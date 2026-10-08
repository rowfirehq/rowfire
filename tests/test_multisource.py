"""Several data sources at once: Postgres and MySQL side by side.

Which source a trigger reads, the worker polling each trigger against its own
database, and the API that manages sources. Needs the control plane and both
fixture databases (`docker compose up -d postgres mysql controlplane`).
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from rowfire import api, sources
from rowfire.definitions import loads as loads_definitions
from rowfire.platform import crypto, scheduler, store
from rowfire.platform.models import TriggerState

PG_DSN = os.environ.get(
    "ROWFIRE_FIXTURE_DSN", "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture"
)
MYSQL_DSN = os.environ.get(
    "ROWFIRE_MYSQL_FIXTURE_DSN", "mysql://rowfire_ro:rowfire_ro@127.0.0.1:3307/rowfire_support"
)
PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)


def _reachable(dsn: str) -> bool:
    try:
        with sources.connect(dsn) as conn:
            conn.fetch("SELECT 1 AS one")
        return True
    except sources.EngineError:
        return False


def _platform() -> bool:
    try:
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM trigger_state LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not (_platform() and _reachable(PG_DSN) and _reachable(MYSQL_DSN)),
        reason="needs the control plane and both fixture databases",
    ),
]

# One trigger on each engine. The Postgres one names no source, so it reads
# the default; the MySQL one names `support`.
DEFS = """
version: 2
triggers:
  order_completed:
    sql: SELECT id, completed_at FROM orders WHERE status = 4 AND completed_at < now()
    event_time: completed_at
    key: [id]
  urgent_ticket:
    source: support
    sql: |
      SELECT `id`, created_at FROM tickets
      WHERE priority = 'urgent' AND is_spam = 0 AND subject NOT LIKE '%test%'
    event_time: created_at
    key: [id]
rules:
  tell_ops: { trigger: order_completed, policy: once_ever }
  page_on_urgent: { trigger: urgent_ticket, policy: once_ever }
"""

URGENT = [n for n in range(1, 481) if n % 7 == 0 and n % 20 != 0]


def _truncate(engine: sqlalchemy.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DO $$
                DECLARE t text;
                BEGIN
                  FOR t IN
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                  LOOP
                    EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', t);
                  END LOOP;
                END $$;
                """
            )
        )


@pytest.fixture
def session():
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    _truncate(engine)
    with Session(engine) as session:
        yield session
        session.rollback()
    engine.dispose()


def _key() -> bytes:
    return base64.urlsafe_b64decode(crypto.generate_master_key())


# ------------------------------------------------------- which source wins


def test_primary_is_the_default_when_it_exists(session) -> None:
    master = _key()
    store.save_connection(session, MYSQL_DSN, name="support", master_key=master)
    store.save_connection(session, PG_DSN, name="primary", master_key=master)
    assert store.default_source(session) == "primary"


def test_a_lone_source_is_the_default_whatever_it_is_called(session) -> None:
    store.save_connection(session, MYSQL_DSN, name="support", master_key=_key())
    assert store.default_source(session) == "support"


def test_adding_a_source_does_not_move_the_default(session) -> None:
    # Regression guard: with "the only source" as the rule, adding a second
    # one left every trigger that names none with no default, and they would
    # stop polling without anything about them having changed.
    master = _key()
    store.save_connection(session, PG_DSN, name="orders", master_key=master)
    session.commit()
    store.save_connection(session, MYSQL_DSN, name="analytics", master_key=master)
    session.commit()
    assert store.default_source(session) == "orders"
    assert store.customer_dsn(session, loads_definitions(DEFS), master_key=master) == PG_DSN


def test_a_trigger_naming_a_missing_source_says_which_exist(session) -> None:
    store.save_connection(session, PG_DSN, name="primary", master_key=_key())
    with pytest.raises(store.StoreError, match="no data source named `support`.*primary"):
        store.trigger_dsn(session, loads_definitions(DEFS), "urgent_ticket", master_key=_key())


def test_each_trigger_resolves_to_its_own_database(session) -> None:
    master = _key()
    store.save_connection(session, PG_DSN, name="primary", master_key=master)
    store.save_connection(session, MYSQL_DSN, name="support", master_key=master)
    definitions = loads_definitions(DEFS)
    assert store.trigger_dsn(session, definitions, "order_completed", master_key=master) == PG_DSN
    assert store.trigger_dsn(session, definitions, "urgent_ticket", master_key=master) == MYSQL_DSN


# ---------------------------------------------------------------- the worker


def test_the_worker_polls_each_trigger_against_its_own_source(session, monkeypatch) -> None:
    master_text = crypto.generate_master_key()
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, master_text)
    master = base64.urlsafe_b64decode(master_text)

    store.save_connection(session, PG_DSN, name="primary", master_key=master)
    store.save_connection(session, MYSQL_DSN, name="support", master_key=master)
    store.save_definitions(session, DEFS)
    session.commit()

    # Rewind both watermarks over the seeded history, so the first poll of
    # each trigger has rows to find.
    now = datetime.now(UTC)
    for state in session.exec(select(TriggerState)).all():
        state.watermark = now - timedelta(days=200)
        state.next_run_at = now - timedelta(seconds=1)
        session.add(state)
    session.commit()

    outcomes = {}
    for _ in range(2):
        outcome = scheduler.tick(session, master_key=master, now=now)
        assert outcome is not None and outcome.error is None, outcome and outcome.error
        outcomes[outcome.trigger_name] = outcome

    with sources.connect(PG_DSN) as pg:
        rows, _ = pg.fetch(
            "SELECT count(*) AS n FROM orders WHERE status = 4 "
            "AND completed_at >= %(a)s AND completed_at < %(b)s",
            {"a": now - timedelta(days=200) - timedelta(seconds=3600), "b": now},
        )
    assert outcomes["order_completed"].matched_rows == rows[0]["n"] > 0
    assert outcomes["urgent_ticket"].matched_rows == len(URGENT)


# ------------------------------------------------------------------- the API


@pytest.fixture
def client(monkeypatch):
    from rowfire.platform import db

    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    monkeypatch.setenv("ROWFIRE_DEMO_MYSQL_DSN", MYSQL_DSN)
    db.reset_engine()
    api.reset_platform_cache()
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    _truncate(engine)
    engine.dispose()

    app = api.create_app(session=api.Session())
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client

    api.reset_platform_cache()
    db.reset_engine()


def _two_sources(client: TestClient) -> None:
    assert client.post("/api/sources", json={"name": "primary", "dsn": PG_DSN}).status_code == 200
    body = client.post("/api/sources", json={"name": "support", "dsn": MYSQL_DSN}).json()
    assert body["kind"] == "mysql"
    assert body["read_only"] is True
    assert body["table_count"] == 2


def test_sources_are_listed_with_their_engine_and_readers(client) -> None:
    _two_sources(client)
    client.post("/api/definitions/save", json={"yaml_text": DEFS})

    listed = client.get("/api/sources").json()
    by_name = {s["name"]: s for s in listed["sources"]}
    assert by_name["primary"]["kind"] == "postgres"
    assert by_name["primary"]["label"] == "PostgreSQL"
    assert by_name["primary"]["default"] is True
    assert by_name["primary"]["triggers"] == ["order_completed"]
    assert by_name["support"]["kind"] == "mysql"
    assert by_name["support"]["triggers"] == ["urgent_ticket"]
    # Never a credential, in any field.
    assert "rowfire_ro" not in str(listed)


def test_health_offers_a_demo_source_per_engine(client) -> None:
    suggested = client.get("/api/health").json()["suggested_sources"]
    assert {"name": "support", "kind": "mysql", "dsn": MYSQL_DSN} in suggested


def test_the_schema_is_read_from_the_source_asked_for(client) -> None:
    _two_sources(client)
    mysql = client.get("/api/schema", params={"source": "support"}).json()
    assert mysql["kind"] == "mysql"
    assert mysql["schema"] == "rowfire_support"
    assert {t["name"] for t in mysql["tables"]} == {"agents", "tickets"}

    postgres = client.get("/api/schema").json()
    assert postgres["source"] == "primary"
    assert "orders" in {t["name"] for t in postgres["tables"]}


def test_a_query_is_checked_in_its_sources_dialect(client) -> None:
    _two_sources(client)
    body = client.post(
        "/api/triggers/check",
        json={
            "sql": "SELECT `id`, created_at FROM `tickets` WHERE subject LIKE '%SSO%'",
            "event_time": "created_at",
            "key": ["id"],
            "source": "support",
        },
    ).json()
    assert body["valid"] is True, body["errors"]
    assert {c["name"]: c["type"] for c in body["columns"]} == {
        "id": "identifier",
        "created_at": "timestamp",
    }
    # Backticks are not Postgres.
    assert (
        client.post(
            "/api/triggers/check",
            json={"sql": "SELECT `id` FROM `tickets`", "key": ["id"], "source": "primary"},
        ).json()["valid"]
        is False
    )


def test_a_trigger_saved_with_a_source_is_backtested_against_it(client) -> None:
    _two_sources(client)
    client.post("/api/definitions/save", json={"yaml_text": DEFS})

    trigger = client.get("/api/definitions").json()["triggers"]
    assert {t["name"]: t["source"] for t in trigger} == {
        "order_completed": None,
        "urgent_ticket": "support",
    }

    result = client.post("/api/backtest", json={"rule": "page_on_urgent", "days": 200}).json()
    assert result["matched_rows"] == len(URGENT)


def test_the_composer_stores_the_source(client) -> None:
    _two_sources(client)
    client.post("/api/definitions/save", json={"yaml_text": DEFS})
    response = client.put(
        "/api/triggers/spam_ticket",
        json={
            "name": "spam_ticket",
            "source": "support",
            "sql": "SELECT id, created_at FROM tickets WHERE is_spam = 1",
            "event_time": "created_at",
            "key": ["id"],
        },
    )
    assert response.status_code == 200, response.text
    yaml_text = client.get("/api/definitions").json()["yaml_text"]
    assert "source: support" in yaml_text
    assert client.get("/api/validate").json() == {"valid": True, "errors": []}


def test_a_source_in_use_cannot_be_deleted(client) -> None:
    _two_sources(client)
    client.post("/api/definitions/save", json={"yaml_text": DEFS})

    refused = client.delete("/api/sources/support")
    assert refused.status_code == 409
    assert "urgent_ticket" in refused.json()["detail"]

    client.delete("/api/rules/page_on_urgent")
    client.delete("/api/triggers/urgent_ticket")
    assert client.delete("/api/sources/support").status_code == 200
    assert [s["name"] for s in client.get("/api/sources").json()["sources"]] == ["primary"]


def test_an_unsupported_database_is_refused_without_echoing_the_dsn(client) -> None:
    response = client.post(
        "/api/sources", json={"name": "warehouse", "dsn": "sqlserver://sa:hunter2@db/prod"}
    )
    assert response.status_code == 400
    assert "hunter2" not in response.text


def test_a_source_name_must_be_an_identifier(client) -> None:
    response = client.post("/api/sources", json={"name": "Support DB", "dsn": MYSQL_DSN})
    assert response.status_code == 422
