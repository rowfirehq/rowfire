"""Every path a hosted demo visitor can take through Get started, end to end.

For each event the SaaS sample offers, and each kind of message: a visitor
saves a rule, backtests it, binds the Demo inbox, turns it on and presses
Simulate new activity -- then the worker polls, and the visitor must find a
delivered message with every placeholder filled in. That is the promise Get
started makes, so it is checked for all of them, not the one that was tried.

Runs the real app, provisioning, simulation and worker against the fixture
Postgres, with the SaaS sample seeded into a template schema of its own.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
import sqlalchemy
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

from rowfire import api, hosted
from rowfire.platform import actions, crypto, scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
SAAS = REPO_ROOT / "examples" / "saas"
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


def _reachable() -> bool:
    try:
        with psycopg.connect(FIXTURE_ADMIN_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM workspace LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="databases unavailable")


def _offered_triggers() -> dict[str, dict]:
    """What Get started offers on a deploy with the sample Postgres only."""
    raw = yaml.safe_load((SAAS / "definitions.yaml").read_text())
    return {
        name: trigger
        for name, trigger in raw["triggers"].items()
        if trigger.get("source", "primary") == "primary"
    }


TRIGGERS = sorted(_offered_triggers())


@pytest.fixture(scope="module")
def template():
    """The SaaS sample, seeded once into a template schema for the module."""
    schema = f"saas_tpl_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(FIXTURE_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        for name in ("01_schema.sql", "02_seed.sql"):
            conn.execute((SAAS / "db" / name).read_bytes())
    yield schema
    with psycopg.connect(FIXTURE_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def visitor(template, monkeypatch):
    """One hosted demo visitor, on the SaaS sample, with a fresh control plane."""
    from rowfire.platform import db

    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    monkeypatch.setenv(hosted.ENABLED_ENV, "1")
    monkeypatch.setenv("ROWFIRE_COOKIE_INSECURE", "1")
    monkeypatch.setenv("ROWFIRE_EGRESS", "inbox-only")
    monkeypatch.setenv("ROWFIRE_DEMO_TEMPLATE_SCHEMA", template)
    monkeypatch.setenv("ROWFIRE_DEMO_DSN", FIXTURE_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_DSN", FIXTURE_ADMIN_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_SQL", str(SAAS / "activity.sql"))
    monkeypatch.setenv("ROWFIRE_HOSTED_DEFINITIONS", str(SAAS / "definitions.yaml"))
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

    app = api.create_app(session=api.Session())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        workspace = client.post("/api/session").json()["workspace"]
        yield client
    hosted.drop_sample(f"visitor_{workspace[:16]}")
    api.reset_platform_cache()
    db.reset_engine()


def _poll_until_idle() -> None:
    """What the worker does: poll every due trigger, in any workspace."""
    from rowfire.platform import db

    master = crypto.load_master_key()
    for _ in range(50):
        with db.session_scope() as session:
            if (
                scheduler.tick(
                    session, master_key=master, deliver=actions.deliver, workspace_id=None
                )
                is None
            ):
                return
    raise AssertionError("the worker never ran out of due triggers")


def _parameters(action: str, columns: list[str], trigger: str) -> dict[str, str]:
    """Templates that use the trigger's own columns, as Get started's do."""
    shown = [c for c in columns if not c.endswith("_id")][:2] or columns[:1]
    email = next((c for c in columns if "email" in c or c == "respondent"), None)
    if action == "post_message":
        return {
            "channel": "#alerts",
            "text": f"{trigger}: " + ", ".join(f"{{{{ {c} }}}}" for c in shown),
        }
    return {
        "subject": f"{trigger}: {{{{ {shown[0]} }}}}",
        "body": "\n".join(f"{c}: {{{{ {c} }}}}" for c in columns),
        "requester_email": f"{{{{ {email} }}}}" if email else "support@example.com",
    }


@pytest.mark.parametrize("action", ["post_message", "create_ticket"])
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_every_event_delivers_after_simulated_activity(visitor, trigger, action) -> None:
    rule = f"{trigger}_check"
    saved = visitor.put(
        f"/api/rules/{rule}",
        json={"name": rule, "trigger": trigger, "policy": "once_per_period", "period": "day"},
    )
    assert saved.status_code == 200, saved.text

    # Get started's step 5: the backtest has to work, and find something.
    backtest = visitor.post("/api/backtest", json={"rule": rule, "days": 90, "sample": 5})
    assert backtest.status_code == 200, backtest.text
    assert backtest.json()["matched_rows"] > 0, f"{trigger} has no history to backtest"
    columns = backtest.json()["columns"]

    visitor.post("/api/integrations", json={"name": "Demo inbox", "provider": "demo_inbox"})
    bound = visitor.post(
        "/api/bindings",
        json={
            "rule_name": rule,
            "integration": "Demo inbox",
            "action": action,
            "parameters": _parameters(action, columns, trigger),
            "recipient_template": "{{ account_id }}" if "account_id" in columns else None,
        },
    )
    assert bound.status_code == 200, bound.text
    assert visitor.post(f"/api/rules/{rule}/mode", json={"mode": "live"}).status_code == 200
    _poll_until_idle()  # the first poll after promotion sets the starting point

    assert visitor.post("/api/demo/activity").status_code == 200
    _poll_until_idle()

    items = [i for i in visitor.get("/api/inbox").json()["items"] if i["rule"] == rule]
    delivered = [i for i in items if i["status"] == "sent"]
    assert delivered, f"{trigger} -> {action}: nothing delivered; got {items}"
    for item in delivered:
        rendered = json.dumps(item["body"])
        assert "{{" not in rendered, f"unfilled placeholder in {rendered}"
        assert item["kind"] == ("message" if action == "post_message" else "ticket")


def test_simulating_again_keeps_delivering(visitor) -> None:
    """A second press is not swallowed by 'at most once a day per account'.

    Each press picks fresh accounts, so a visitor who presses twice sees more
    arrive -- for the event Get started lists first and is most often tried.
    """
    rule = "declines"
    visitor.put(
        f"/api/rules/{rule}",
        json={
            "name": rule,
            "trigger": "payment_failed",
            "policy": "once_per_period",
            "period": "day",
        },
    )
    visitor.post("/api/integrations", json={"name": "Demo inbox", "provider": "demo_inbox"})
    visitor.post(
        "/api/bindings",
        json={
            "rule_name": rule,
            "integration": "Demo inbox",
            "action": "post_message",
            "parameters": {"channel": "#billing", "text": "{{ account }}"},
            "recipient_template": "{{ account_id }}",
        },
    )
    visitor.post(f"/api/rules/{rule}/mode", json={"mode": "live"})
    _poll_until_idle()

    counts = []
    for _ in range(3):
        visitor.post("/api/demo/activity")
        _poll_until_idle()
        counts.append(
            sum(1 for i in visitor.get("/api/inbox").json()["items"] if i["status"] == "sent")
        )
    assert counts[0] > 0 and counts[2] > counts[0], counts
