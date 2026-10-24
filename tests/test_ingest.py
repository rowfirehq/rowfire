"""Pushed events: the ingest server, the events source, and the whole loop.

Requires the control-plane Postgres. The last test runs the real in-app SDK
(sdk/python) against a real ingest server and lets the scheduler fire on what
arrives -- the path an app that never grants a database login actually takes.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import socket
import threading
import time
import uuid

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import Integer, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as AppSession
from sqlmodel import Session, select

from rowfire import sources
from rowfire.ingest import create_ingest_app
from rowfire.platform import crypto, scheduler, store
from rowfire.platform import db as platform_db
from rowfire.platform import events as event_store
from rowfire.platform.models import DEFAULT_WORKSPACE_ID, Fire, IngestedEvent

PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)
OTHER_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

DEFINITIONS = """
version: 2
subscriptions:
  goodwill_refund:
    operation: POST /admin/charges/{id}/refund
    write: {table: charges, kind: [update], changed: status}
    where: {row.status: refunded, actor.role: support}
    fields: [row.id, row.amount, old.status, actor.id]
triggers:
  goodwill_refund:
    source: app_events
    sql: |
      SELECT event_id, received_at, occurred_at,
             data->>'row.id' AS charge_id,
             data->>'actor.id' AS agent
      FROM events
      WHERE subscription = 'goodwill_refund'
    event_time: received_at
    key: [charge_id]
rules:
  thank_the_agent:
    trigger: goodwill_refund
    policy: once_ever
"""


def _available() -> bool:
    try:
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM ingested_event LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="control-plane database unavailable")


@pytest.fixture
def engine():
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
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
    platform_db.reset_engine()
    yield engine
    platform_db.reset_engine()
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture
def api_key(session) -> str:
    _, secret = event_store.create_key(session, "test app")
    session.commit()
    return secret


@pytest.fixture
def http(engine):
    with TestClient(create_ingest_app()) as client:
        yield client


def _event(n: int, **extra) -> dict:
    return {
        "id": f"evt{n:029d}",
        "subscription": "goodwill_refund",
        "operation": "POST /admin/charges/{id}/refund",
        "occurred_at": "2026-10-24T10:00:00+00:00",
        "data": {"row.id": n, "actor.id": "agent-7"},
        **extra,
    }


def _post(http, path: str, body, key: str | None, **headers):
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return http.post(path, content=raw, headers=headers)


# ------------------------------------------------------------------ keys


def test_every_request_needs_a_live_key(http, session, api_key) -> None:
    batch = {"events": [_event(1)]}
    assert _post(http, "/v1/events", batch, None).status_code == 401
    assert _post(http, "/v1/events", batch, "rf_not-a-real-key").status_code == 401
    assert _post(http, "/v1/events", batch, api_key).status_code == 202

    event_store.revoke_key(session, api_key[:11])
    session.commit()
    assert _post(http, "/v1/events", batch, api_key).status_code == 401


def test_only_a_hash_of_the_key_is_stored(session, api_key) -> None:
    [key] = event_store.list_keys(session)
    assert api_key not in (key.key_hash, key.key_prefix)
    assert api_key.startswith(key.key_prefix)


# ---------------------------------------------------------------- events


def test_a_batch_and_its_retry_are_stored_once(http, session, api_key) -> None:
    batch = {"events": [_event(1), _event(2)]}
    first = _post(http, "/v1/events", batch, api_key).json()
    retry = _post(http, "/v1/events", batch, api_key).json()
    assert first == {"stored": 2, "duplicates": 0}
    assert retry == {"stored": 0, "duplicates": 2}

    rows = session.exec(select(IngestedEvent)).all()
    assert len(rows) == 2
    assert all(row.received_at is not None for row in rows)
    assert rows[0].data == {"row.id": 1, "actor.id": "agent-7"}


def test_gzip_bodies_are_accepted(http, api_key) -> None:
    body = gzip.compress(json.dumps({"events": [_event(1)]}).encode())
    response = _post(http, "/v1/events", body, api_key, **{"Content-Encoding": "gzip"})
    assert response.json() == {"stored": 1, "duplicates": 0}


def test_a_small_body_that_inflates_past_the_limit_is_refused(http, api_key) -> None:
    bomb = gzip.compress(b"[" + b" " * (25 * 1024 * 1024) + b"]")
    assert len(bomb) < 100_000
    response = _post(http, "/v1/events", bomb, api_key, **{"Content-Encoding": "gzip"})
    assert response.status_code == 413


def test_malformed_batches_are_refused_with_400(http, api_key) -> None:
    # 400, which the SDK drops and counts, because resending cannot fix it.
    assert _post(http, "/v1/events", b"not json", api_key).status_code == 400
    missing_id = {"events": [{k: v for k, v in _event(1).items() if k != "id"}]}
    assert _post(http, "/v1/events", missing_id, api_key).status_code == 400


def test_an_sdks_losses_are_added_up_per_day(http, session, api_key) -> None:
    _post(http, "/v1/events", {"events": [], "lost": {"queue_overflow": 3}}, api_key)
    _post(
        http, "/v1/events", {"events": [], "lost": {"queue_overflow": 2, "spool_error": 1}}, api_key
    )
    totals = {
        row["reason"]: row["count"]
        for row in event_store.losses(session, workspace_id=DEFAULT_WORKSPACE_ID)
    }
    assert totals == {"queue_overflow": 5, "spool_error": 1}


def test_a_key_writes_only_to_its_own_workspace(http, session) -> None:
    store.ensure_workspace(session, OTHER_WORKSPACE)
    _, other_key = event_store.create_key(session, "other", workspace_id=OTHER_WORKSPACE)
    session.commit()
    _post(http, "/v1/events", {"events": [_event(1)]}, other_key)
    [row] = session.exec(select(IngestedEvent)).all()
    assert row.workspace_id == OTHER_WORKSPACE


# --------------------------------------------------------------- catalog


def _catalog(instance: str, seen: int) -> dict:
    return {
        "instance": {"id": instance, "started_at": "2026-10-24T09:00:00+00:00"},
        "operations": {
            "POST /checkout": {
                "declared": True,
                "seen": seen,
                "errors": 0,
                "statuses": {"200": seen},
                "params": {},
                "actor": {"role": {"values": [{"value": "customer", "seen": seen}]}},
                "writes": [
                    {
                        "table": "charges",
                        "kind": "insert",
                        "seen": seen,
                        "sampled": seen,
                        "changed": {"status": seen},
                    }
                ],
            }
        },
        "tables": {
            "charges": {
                "columns": {
                    "status": {"type": "string", "values": [{"value": "paid", "seen": seen}]}
                },
                "transitions": {},
            }
        },
    }


def test_catalogs_are_kept_per_process_and_added_up(http, session, api_key) -> None:
    assert _post(http, "/v1/catalog", _catalog("web-1", 3), api_key).status_code == 202
    assert _post(http, "/v1/catalog", _catalog("web-2", 4), api_key).status_code == 202
    # web-1 again: its counts are cumulative, so this replaces its last one.
    assert _post(http, "/v1/catalog", _catalog("web-1", 5), api_key).status_code == 202

    merged = event_store.merged_catalog(session, workspace_id=DEFAULT_WORKSPACE_ID)
    op = merged["operations"]["POST /checkout"]
    assert merged["instances"] == 2
    assert op["seen"] == 9
    assert op["writes"] == [
        {"table": "charges", "kind": "insert", "seen": 9, "sampled": 9, "changed": {"status": 9}}
    ]
    assert op["actor"]["role"] == {"values": [{"value": "customer", "seen": 9}]}
    assert merged["tables"]["charges"]["columns"]["status"] == {
        "type": "string",
        "values": [{"value": "paid", "seen": 9}],
    }


def test_a_catalog_without_an_instance_is_refused(http, api_key) -> None:
    assert _post(http, "/v1/catalog", {"operations": {}}, api_key).status_code == 400


# ---------------------------------------------------------------- config


def test_config_serves_the_active_subscriptions_with_an_etag(http, session, api_key) -> None:
    empty = http.get("/v1/config", headers={"Authorization": f"Bearer {api_key}"})
    assert empty.json() == {"version": None, "subscriptions": []}

    store.save_definitions(session, DEFINITIONS)
    session.commit()
    response = http.get("/v1/config", headers={"Authorization": f"Bearer {api_key}"})
    body = response.json()
    assert body["version"] == "1"
    assert body["subscriptions"] == [
        {
            "id": "goodwill_refund",
            "operation": "POST /admin/charges/{id}/refund",
            "write": {"table": "charges", "kind": ["update"], "changed": "status"},
            "where": {"row.status": "refunded", "actor.role": "support"},
            "fields": ["row.id", "row.amount", "old.status", "actor.id"],
        }
    ]
    unchanged = http.get(
        "/v1/config",
        headers={"Authorization": f"Bearer {api_key}", "If-None-Match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304


# --------------------------------------------------------- events source


def _store(session, workspace_id, *ns) -> None:
    event_store.store_events(session, [_event(n) for n in ns], workspace_id=workspace_id)
    session.commit()


def test_the_events_source_sees_only_its_own_workspace(session) -> None:
    store.ensure_workspace(session, OTHER_WORKSPACE)
    _store(session, DEFAULT_WORKSPACE_ID, 1, 2)
    _store(session, OTHER_WORKSPACE, 3)

    mine = sources.pin_workspace(sources.EVENTS_DSN, DEFAULT_WORKSPACE_ID)
    with sources.connect(mine) as conn:
        rows, _ = conn.fetch("SELECT data->>'row.id' AS id FROM events ORDER BY seq")
    assert [row["id"] for row in rows] == ["1", "2"]


def test_the_events_source_reads_no_other_table(session) -> None:
    dsn = sources.pin_workspace(sources.EVENTS_DSN, DEFAULT_WORKSPACE_ID)
    with sources.connect(dsn) as conn:
        for query in (
            "SELECT * FROM connection",
            "SELECT * FROM ingested_event",
            "SELECT * FROM public.events",
            "SELECT query_to_xml('select * from connection', true, true, '')",
            "SELECT e.* FROM events e JOIN integration i ON true",
        ):
            with pytest.raises(sources.EngineError):
                conn.fetch(query)
        with pytest.raises(sources.EngineError):
            conn.cursor()


def test_the_events_source_is_read_only(session) -> None:
    dsn = sources.pin_workspace(sources.EVENTS_DSN, DEFAULT_WORKSPACE_ID)
    with sources.connect(dsn) as conn:
        info = conn.server_info()
        assert info["read_only"] is True
        with pytest.raises(sources.EngineError):
            conn.fetch("DELETE FROM events")


def test_a_stored_events_source_reads_its_connections_workspace_whatever_it_says(
    session,
) -> None:
    # A DSN naming another workspace is overruled by the connection's own.
    master = base64.urlsafe_b64decode(crypto.generate_master_key())
    sneaky = f"rowfire://events?workspace={OTHER_WORKSPACE}"
    store.save_connection(session, sneaky, name="app_events", master_key=master)
    session.commit()
    connection = store.get_connection(session, name="app_events")
    revealed = store.reveal_dsn(connection, master_key=master)
    assert revealed == f"rowfire://events?workspace={DEFAULT_WORKSPACE_ID}"


def test_an_events_source_without_a_workspace_refuses_to_open() -> None:
    with pytest.raises(sources.EngineError, match="without a workspace"):
        with sources.connect(sources.EVENTS_DSN):
            pass


# ------------------------------------------------------------ whole loop


class AppBase(DeclarativeBase):
    """The customer app's models, in its own database."""


class Charge(AppBase):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ingest_server(engine):
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_ingest_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_an_app_that_grants_no_database_login_still_fires(
    session, api_key, ingest_server, monkeypatch
) -> None:
    """SDK in the app -> ingest server -> events source -> scheduler -> fire."""
    pytest.importorskip("rowfire_sdk")
    import rowfire_sdk
    from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy

    master = base64.urlsafe_b64decode(crypto.generate_master_key())
    store.save_definitions(session, DEFINITIONS)
    store.save_connection(session, sources.EVENTS_DSN, name="app_events", master_key=master)
    session.commit()

    # The app's own database, which Rowfire never connects to.
    app_db = sqlalchemy.create_engine("sqlite://")
    AppBase.metadata.create_all(app_db)
    with AppSession(app_db) as s:
        s.add_all([Charge(id=1, status="paid"), Charge(id=2, status="paid")])
        s.commit()

    # Subscriptions come from the ingest server's /v1/config, not from code.
    client = rowfire_sdk.init(
        ingest_server, api_key, flush_interval=0.05, flush_jitter=0, config_interval=0.05
    )
    rowfire_sqlalchemy.instrument()
    try:
        deadline = time.monotonic() + 10
        while client.matcher.version != "1" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert client.matcher.version == "1", client.stats()

        for charge_id, role in ((1, "support"), (2, "admin")):  # only support counts
            with rowfire_sdk.operation("POST /admin/charges/{id}/refund"):
                rowfire_sdk.set_actor(id="agent-7", role=role)
                with AppSession(app_db) as s:
                    s.get(Charge, charge_id).status = "refunded"
                    s.commit()
        assert rowfire_sdk.flush(timeout=10)
    finally:
        rowfire_sdk.close(timeout=2)
        rowfire_sqlalchemy.uninstrument()

    assert event_store.count_events(session, workspace_id=DEFAULT_WORKSPACE_ID) == 1

    outcome = scheduler.tick(session, master_key=master)
    assert outcome is not None and outcome.error is None, outcome
    assert outcome.matched_rows == 1
    assert outcome.fires_new == 1

    [fire] = session.exec(select(Fire)).all()
    assert fire.rule_name == "thank_the_agent"
    assert fire.dedup_key == '["1"]'

    # Polled again, the same event does not fire twice.
    state = session.exec(select(scheduler.TriggerState)).one()
    state.next_run_at = None
    session.add(state)
    session.commit()
    again = scheduler.tick(session, master_key=master)
    assert again is not None and again.fires_new == 0 and again.fires_suppressed == 1


# ------------------------------------------------------------ through the UI


@pytest.fixture
def ui(engine, monkeypatch):
    from rowfire import api

    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    api.reset_platform_cache()
    with TestClient(api.create_app(session=api.Session()), base_url="http://127.0.0.1") as client:
        yield client
    api.reset_platform_cache()


def test_the_ui_adds_an_events_source_and_shows_what_arrived(ui, session) -> None:
    added = ui.post("/api/sources", json={"name": "app_events", "dsn": "rowfire://events"})
    assert added.status_code == 200, added.text
    body = added.json()
    assert (body["kind"], body["table_count"], body["read_only"]) == ("events", 1, True)

    listed = ui.get("/api/sources").json()["sources"]
    assert [(s["name"], s["label"]) for s in listed] == [("app_events", "App events")]

    created = ui.post("/api/app-events/keys", json={"name": "web"}).json()
    assert created["key"].startswith(created["prefix"])
    assert "key" not in ui.get("/api/app-events/keys").json()["keys"][0]

    _store(session, DEFAULT_WORKSPACE_ID, 1, 2)
    recent = ui.get("/api/app-events/recent").json()
    assert recent["total"] == 2
    assert [e["data"]["row.id"] for e in recent["events"]] == [2, 1]


def test_the_app_events_endpoints_do_not_exist_in_the_hosted_demo(ui, monkeypatch) -> None:
    from rowfire import hosted

    monkeypatch.setattr(hosted, "enabled", lambda: True)
    for path in ("/api/app-events/catalog", "/api/app-events/recent", "/api/app-events/keys"):
        assert ui.get(path).status_code in (401, 404), path
    created = ui.post("/api/app-events/keys", json={"name": "visitor"})
    assert created.status_code in (401, 403, 404)


def test_the_events_source_follows_the_control_planes_search_path(session, monkeypatch) -> None:
    # On a cloud deploy the control plane lives in its own schema, not public.
    session.execute(text("DROP SCHEMA IF EXISTS elsewhere CASCADE"))
    session.execute(text("CREATE SCHEMA elsewhere"))
    session.execute(
        text("CREATE TABLE elsewhere.ingested_event (LIKE public.ingested_event INCLUDING ALL)")
    )
    session.execute(
        text(
            "INSERT INTO elsewhere.ingested_event (workspace_id, event_id, subscription, data) "
            "VALUES (:ws, 'elsewhere0000001', 'from_elsewhere', '{}')"
        ),
        {"ws": DEFAULT_WORKSPACE_ID},
    )
    session.commit()
    try:
        base = PLATFORM_DSN.replace("postgresql+psycopg://", "postgresql://")
        monkeypatch.setenv(
            sources.EVENTS_READER_DSN_ENV, f"{base}?options=-csearch_path%3Delsewhere"
        )
        dsn = sources.pin_workspace(sources.EVENTS_DSN, DEFAULT_WORKSPACE_ID)
        with sources.connect(dsn) as conn:
            rows, _ = conn.fetch("SELECT subscription FROM events")
        assert rows == [{"subscription": "from_elsewhere"}]
    finally:
        session.execute(text("DROP SCHEMA elsewhere CASCADE"))
        session.commit()


# ------------------------------------------------------------------ CLI


def test_the_cli_shows_what_arrived(session) -> None:
    from click.testing import CliRunner

    from rowfire.cli import cli

    _store(session, DEFAULT_WORKSPACE_ID, 1, 2)
    event_store.save_catalog(session, _catalog("web-1", 3), workspace_id=DEFAULT_WORKSPACE_ID)
    event_store.record_losses(session, {"queue_overflow": 4}, workspace_id=DEFAULT_WORKSPACE_ID)
    session.commit()
    runner = CliRunner()

    recent = runner.invoke(cli, ["platform", "events", "recent", "--json"])
    assert recent.exit_code == 0, recent.output
    lines = [json.loads(line) for line in recent.output.splitlines()]
    assert [line["data"]["row.id"] for line in lines] == [2, 1]

    table = runner.invoke(cli, ["platform", "events", "recent"])
    assert "goodwill_refund" in table.output and "2 event(s) stored" in table.output

    catalog = runner.invoke(cli, ["platform", "events", "catalog"])
    assert "POST /checkout" in catalog.output and "insert charges x3" in catalog.output
    merged = json.loads(runner.invoke(cli, ["platform", "events", "catalog", "--json"]).output)
    assert merged["operations"]["POST /checkout"]["seen"] == 3

    losses = runner.invoke(cli, ["platform", "events", "losses"])
    assert "queue_overflow" in losses.output and "4" in losses.output


def test_the_cli_creates_lists_and_revokes_keys(session) -> None:
    from click.testing import CliRunner

    from rowfire.cli import cli

    runner = CliRunner()
    created = runner.invoke(cli, ["platform", "ingest-key", "create", "--name", "web"])
    assert created.exit_code == 0, created.output
    [key] = event_store.list_keys(session)
    assert key.key_prefix in created.output

    revoked = runner.invoke(cli, ["platform", "ingest-key", "revoke", key.key_prefix])
    assert revoked.exit_code == 0, revoked.output
    listing = runner.invoke(cli, ["platform", "ingest-key", "list"])
    assert "revoked" in listing.output
