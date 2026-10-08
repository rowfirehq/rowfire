"""The hosted demo: a private workspace per visitor, on one shared instance.

These run the real middleware, the real provisioning (a copy of the fixture
tables in a schema of the visitor's own) and the real reaper, against the
fixture database standing in for the sample one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import text

from rowfire import api, hosted
from rowfire.platform import crypto
from rowfire.platform.models import Workspace

FIXTURE_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture",
)
FIXTURE_ADMIN_DSN = os.environ.get(
    "TEST_FIXTURE_ADMIN_URL",
    "postgresql://rowfire:rowfire@localhost:5433/rowfire_fixture",
)
PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _reachable() -> bool:
    try:
        with psycopg.connect(FIXTURE_ADMIN_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1 FROM orders LIMIT 1")
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM workspace LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="databases unavailable")


def _visitor_schemas() -> set[str]:
    with psycopg.connect(FIXTURE_ADMIN_DSN) as conn:
        rows = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'visitor\\_%'"
        ).fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def demo(monkeypatch, tmp_path):
    """A hosted app on an empty control plane; yields a factory of visitors."""
    from rowfire.platform import db

    activity = tmp_path / "activity.sql"
    # Lands in whichever schema search_path names first: the visitor's own.
    activity.write_text("CREATE TABLE simulated_here (n int);\n")

    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    monkeypatch.setenv(hosted.ENABLED_ENV, "1")
    monkeypatch.setenv("ROWFIRE_COOKIE_INSECURE", "1")
    monkeypatch.setenv("ROWFIRE_DEMO_DSN", FIXTURE_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_DSN", FIXTURE_ADMIN_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_SQL", str(activity))
    monkeypatch.setenv("ROWFIRE_HOSTED_DEFINITIONS", str(REPO_ROOT / "definitions.yaml"))
    monkeypatch.delenv("ROWFIRE_DEMO_MYSQL_DSN", raising=False)
    monkeypatch.setattr(hosted, "_sessions_by_address", hosted._Throttle())
    monkeypatch.setattr(hosted, "_recent", hosted._Recent())
    monkeypatch.setattr(hosted, "_activity_by_workspace", hosted._Throttle())
    db.reset_engine()
    api.reset_platform_cache()

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
    engine.dispose()
    before = _visitor_schemas()

    app = api.create_app(session=api.Session())
    clients: list[TestClient] = []

    def visitor(with_session: bool = True) -> TestClient:
        client = TestClient(app, base_url="http://127.0.0.1")
        client.__enter__()
        clients.append(client)
        if with_session:
            assert client.post("/api/session").status_code == 200
        return client

    yield visitor

    for client in clients:
        client.__exit__(None, None, None)
    for schema in _visitor_schemas() - before:
        hosted.drop_sample(schema)
    api.reset_platform_cache()
    db.reset_engine()


def test_a_visitor_without_a_session_gets_nothing_but_health(demo) -> None:
    stranger = demo(with_session=False)

    health = stranger.get("/api/health").json()
    assert health["hosted"] is True and health["session"] is False
    assert health["connected"] is False
    response = stranger.get("/api/triggers")
    assert response.status_code == 401
    assert response.json()["session_required"] is True


def test_a_new_visitor_starts_with_the_sample_set_up(demo) -> None:
    visitor = demo()

    health = visitor.get("/api/health").json()
    assert health["session"] is True and health["connected"] is True
    assert health["suggested_sources"] == []
    assert [s["name"] for s in visitor.get("/api/sources").json()["sources"]] == ["primary"]
    names = {t["name"] for t in visitor.get("/api/definitions").json()["triggers"]}
    assert "order_completed" in names
    # And it reads their own copy of the tables, not the shared original.
    backtest = visitor.post(
        "/api/backtest", json={"rule": "tell_ops_every_completed_order", "days": 3650}
    )
    assert backtest.status_code == 200, backtest.text
    assert backtest.json()["matched_rows"] > 0


def test_visitors_cannot_see_or_touch_each_others_work(demo) -> None:
    alice, bob = demo(), demo()

    created = alice.post(
        "/api/integrations", json={"name": "Alice inbox", "provider": "demo_inbox"}
    ).json()
    alice.put(
        "/api/rules/alices_rule",
        json={"name": "alices_rule", "trigger": "order_completed", "policy": "once_ever"},
    )

    assert bob.get("/api/integrations").json()["integrations"] == []
    assert "alices_rule" not in {r["name"] for r in bob.get("/api/definitions").json()["rules"]}
    # An id is not a key: Bob gets the same answer as for one that does not exist.
    assert bob.delete(f"/api/integrations/{created['id']}").status_code == 404
    assert [i["name"] for i in alice.get("/api/integrations").json()["integrations"]] == [
        "Alice inbox"
    ]


def test_data_sources_are_fixed(demo) -> None:
    visitor = demo()
    for response in (
        visitor.post("/api/sources", json={"name": "mine", "dsn": "postgresql://x@10.0.0.1/y"}),
        visitor.post("/api/connect", json={"dsn": "postgresql://x@169.254.169.254/y"}),
        visitor.delete("/api/sources/primary"),
    ):
        assert response.status_code == 403


def test_a_cross_site_write_is_refused(demo) -> None:
    visitor = demo()
    response = visitor.post(
        "/api/halt", json={"reason": "csrf"}, headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert visitor.get("/api/rules").json()["halted"] is False


def test_a_forged_cookie_is_no_session(demo) -> None:
    stranger = demo(with_session=False)
    stranger.cookies.set(hosted.COOKIE, "0" * 32 + ".deadbeef")
    assert stranger.get("/api/triggers").status_code == 401


def test_simulated_activity_lands_in_the_visitors_own_copy(demo) -> None:
    visitor = demo()
    assert visitor.post("/api/demo/activity").status_code == 200

    with psycopg.connect(FIXTURE_ADMIN_DSN) as conn:
        found = {
            row[0]
            for row in conn.execute(
                "SELECT schemaname FROM pg_tables WHERE tablename = 'simulated_here'"
            ).fetchall()
        }
    assert len(found) == 1 and next(iter(found)).startswith("visitor_")


def test_an_idle_visitor_is_reaped_with_their_sample_data(demo) -> None:
    from rowfire.platform import db

    visitor = demo()
    workspace_hex = visitor.post("/api/session").json()["workspace"]
    schema = f"visitor_{workspace_hex[:16]}"
    assert schema in _visitor_schemas()

    with db.session_scope() as session:
        removed = hosted.reap(session, now=datetime.now(UTC) + timedelta(hours=48))
    assert removed == 1

    with db.session_scope() as session:
        from sqlmodel import select

        assert session.exec(select(Workspace).where(Workspace.ephemeral)).all() == []
    assert schema not in _visitor_schemas()
    assert visitor.get("/api/triggers").status_code == 401


def test_simulated_activity_is_rate_limited_per_visitor(demo, monkeypatch) -> None:
    monkeypatch.setattr(hosted, "SIMULATIONS_PER_HOUR", 1)
    alice, bob = demo(), demo()

    assert alice.post("/api/demo/activity").status_code == 200
    assert alice.post("/api/demo/activity").status_code == 429
    # Per visitor: Alice's limit is not Bob's.
    assert bob.post("/api/demo/activity").status_code == 200


def test_a_visitors_copy_owns_its_id_sequences(demo) -> None:
    """So the template can be dropped and reseeded without breaking anyone.

    A cloud deploy reseeds the sample on every deploy; a copy whose ids still
    came from the template's sequences would lose its defaults with it.
    """
    visitor = demo()
    schema = f"visitor_{visitor.post('/api/session').json()['workspace'][:16]}"

    with psycopg.connect(FIXTURE_ADMIN_DSN) as conn:
        defaults = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = %s AND column_default LIKE 'nextval(%%'",
            (schema,),
        ).fetchall()
        assert defaults
        assert all(schema in default for (default,) in defaults)
        # And they start past the copied rows.
        conn.execute(f'SET search_path TO "{schema}"')
        before = conn.execute("SELECT max(id) FROM orders").fetchone()[0]
        conn.execute(
            "INSERT INTO orders (customer_id, service_id, status) "
            "SELECT customer_id, service_id, 4 FROM orders LIMIT 1"
        )
        assert conn.execute("SELECT max(id) FROM orders").fetchone()[0] > before
        conn.rollback()


def test_a_workspace_reaped_by_another_process_is_gone_at_once(demo) -> None:
    """The reaper runs in the worker; the web process must not cache it alive.

    It did, for five minutes, and in that window the visitor's requests ran
    against a workspace that no longer existed -- recreating it as a
    permanent, never-reaped one.
    """
    from rowfire.platform import db

    visitor = demo()
    assert visitor.get("/api/triggers").status_code == 200  # cached as live here
    workspace_id = visitor.post("/api/session").json()["workspace"]

    # What the worker's reaper does, without touching this process's cache.
    import uuid as _uuid

    with db.session_scope() as session:
        hosted.delete_workspace(session, _uuid.UUID(hex=workspace_id))
    hosted._recent.mark(_uuid.UUID(hex=workspace_id))  # as if just touched

    assert visitor.get("/api/triggers").status_code == 401
    with db.session_scope() as session:
        assert session.get(Workspace, _uuid.UUID(hex=workspace_id)) is None
