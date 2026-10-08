"""The HTTP surface.

Everything it reads or writes lives in the control plane: definitions as
versions, the customer connection encrypted, integrations and their actions.
There is no file mode and no fallback, so there is one set of tests rather
than one per mode.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import text

from rowfire import api
from rowfire.platform.models import Mode

FIXTURE_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture",
)
PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)
REPO_ROOT = Path(__file__).resolve().parents[1]

DEFS = (REPO_ROOT / "definitions.yaml").read_text()


def _reachable(dsn: str, probe: str) -> bool:
    try:
        engine = sqlalchemy.create_engine(dsn)
        with engine.connect() as conn:
            conn.execute(text(probe))
        engine.dispose()
        return True
    except Exception:
        return False


needs_fixture = pytest.mark.skipif(
    not _reachable(
        FIXTURE_DSN.replace("postgresql://", "postgresql+psycopg://"),
        "SELECT 1 FROM orders LIMIT 1",
    ),
    reason="fixture database unavailable",
)
needs_platform = pytest.mark.skipif(
    not _reachable(PLATFORM_DSN, "SELECT 1 FROM trigger_state LIMIT 1"),
    reason="control-plane database unavailable",
)


@pytest.fixture
def persistent(monkeypatch):
    """The only mode: everything lives in the control plane."""
    from rowfire.platform import crypto, db

    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
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
                  -- Every table, so adding one later cannot silently leak
                  -- state between tests the way the integration tables did.
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
    # TestClient defaults to Host: testserver, which the loopback guard
    # correctly rejects with 421. Point it at a loopback host instead.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client

    api.reset_platform_cache()
    db.reset_engine()


# ------------------------------------------------------------------ basics


@needs_platform
def test_loopback_guard_still_applies(persistent) -> None:
    # Not incidental: this is what stops any page the user browses from
    # driving the server against their production replica.
    response = persistent.get("/api/health", headers={"host": "evil.example.com"})
    assert response.status_code == 421


def test_an_allowed_host_is_accepted_by_exact_name_only(monkeypatch) -> None:
    # A forwarded port reaches the server under a name such as
    # <name>-8000.app.github.dev. Naming it lets that through; nothing that
    # merely looks like it gets in.
    monkeypatch.setenv(api.ALLOWED_HOSTS_ENV, " Demo-8000.app.github.dev ,")
    allowed = api._allowed_hosts()

    assert api._host_allowed("demo-8000.app.github.dev", allowed)
    assert api._host_allowed("demo-8000.app.github.dev:443", allowed)
    assert api._host_allowed("127.0.0.1:8000", allowed)
    assert not api._host_allowed("evil-8000.app.github.dev", allowed)
    assert not api._host_allowed("demo-8000.app.github.dev.evil.example", allowed)
    assert not api._host_allowed("x.demo-8000.app.github.dev", allowed)


def test_no_extra_host_is_allowed_by_default(monkeypatch) -> None:
    monkeypatch.delenv(api.ALLOWED_HOSTS_ENV, raising=False)
    assert api._allowed_hosts() == frozenset()
    assert not api._host_allowed("demo-8000.app.github.dev", api._allowed_hosts())


@needs_platform
def test_the_guard_reads_allowed_hosts_when_the_app_starts(persistent, monkeypatch) -> None:
    monkeypatch.setenv(api.ALLOWED_HOSTS_ENV, "demo-8000.app.github.dev")
    app = api.create_app(session=api.Session())
    with TestClient(app, base_url="https://demo-8000.app.github.dev") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 421


@needs_platform
def test_health_reports_the_stored_version(persistent) -> None:
    # Null until something is stored, which is the UI's cue to offer to
    # generate a starting set rather than to render an empty list as if that
    # were the answer.
    assert persistent.get("/api/health").json()["definitions_version"] is None

    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    assert persistent.get("/api/health").json()["definitions_version"] == 1


@needs_platform
def test_reading_definitions_before_any_are_stored_says_so(persistent) -> None:
    response = persistent.get("/api/definitions")
    assert response.status_code == 404
    # And names the two ways out rather than just refusing.
    detail = response.json()["detail"]
    assert "platform push" in detail


@needs_platform
def test_endpoints_refuse_while_the_control_plane_is_down(persistent, monkeypatch) -> None:
    # 503, not 501: the request is implemented and will work again on retry.
    # It cannot mean "you have no control plane" any more -- the server
    # refuses to start without one.
    monkeypatch.setattr(api, "platform_ready", lambda: False)
    response = persistent.get("/api/triggers")
    assert response.status_code == 503
    assert "not answering" in response.json()["detail"]


# ------------------------------------------------------------- persistence


@needs_fixture
@needs_platform
def test_connect_persists_the_credential_encrypted(persistent) -> None:
    # Unconditional: the worker reads this same connection, so a DSN that
    # lived only in the API process would mean the UI and the scheduler
    # querying different databases.
    body = persistent.post("/api/connect", json={"dsn": FIXTURE_DSN}).json()
    assert body["stored"] is True

    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine.connect() as conn:
        blob = conn.execute(text("SELECT dsn_ciphertext FROM connection")).scalar_one()
    engine.dispose()
    # The password from the DSN must not be recoverable from the column.
    assert b"rowfire_ro" not in blob


@needs_fixture
@needs_platform
def test_save_versions_definitions_and_creates_state(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    body = persistent.post("/api/definitions/save", json={"yaml_text": DEFS}).json()
    assert body["version"] == 1

    triggers = persistent.get("/api/triggers").json()["triggers"]
    rules = persistent.get("/api/rules").json()["rules"]
    assert len(triggers) == body["triggers"]
    assert len(rules) == body["rules"]

    # Scheduling belongs to the trigger...
    assert all(t["watermark"] is not None for t in triggers)
    # ...and mode belongs to the rule, which always arrives in shadow.
    assert all(r["mode"] == Mode.shadow.value for r in rules)


@needs_fixture
@needs_platform
def test_a_trigger_reports_the_rules_hanging_off_it(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    triggers = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    # definitions.yaml deliberately puts two rules at different cadences on
    # one trigger, which is the shape the whole split exists to support.
    assert len(triggers["customer_ordered"]["rules"]) >= 2
    assert triggers["customer_ordered"]["live_rules"] == []


@needs_fixture
@needs_platform
def test_a_removed_rule_stops_counting_against_its_trigger(persistent) -> None:
    # reconcile disables a removed rule rather than deleting it, so the fires
    # it already wrote keep their meaning. A tombstone is not a dependent,
    # though, and counting one made a trigger look busier than it is.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    before = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert "nudge_ops_daily" in before["customer_ordered"]["rules"]

    persistent.delete("/api/rules/nudge_ops_daily")

    after = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert "nudge_ops_daily" not in after["customer_ordered"]["rules"]
    # ...but the rule itself is still listed, disabled, with its history.
    rules = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert rules["nudge_ops_daily"]["enabled"] is False


@needs_fixture
@needs_platform
def test_saving_again_creates_a_new_version(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    second = persistent.post("/api/definitions/save", json={"yaml_text": DEFS}).json()
    assert second["version"] == 2


@needs_fixture
@needs_platform
def test_promote_and_demote_round_trip(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    promoted = persistent.post(
        "/api/rules/tell_ops_every_completed_order/mode", json={"mode": "live"}
    )
    assert promoted.json()["mode"] == "live"

    demoted = persistent.post(
        "/api/rules/tell_ops_every_completed_order/mode", json={"mode": "shadow"}
    )
    assert demoted.json()["mode"] == "shadow"


@needs_fixture
@needs_platform
def test_rules_on_one_trigger_are_promoted_independently(persistent) -> None:
    # The product reason for the split: notifying ops can go live while
    # texting the customer is still being watched.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    persistent.post("/api/rules/nudge_ops_daily/mode", json={"mode": "live"})

    rules = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert rules["nudge_ops_daily"]["mode"] == "live"
    assert rules["thank_the_customer_weekly"]["mode"] == "shadow"
    assert rules["nudge_ops_daily"]["trigger"] == rules["thank_the_customer_weekly"]["trigger"]

    triggers = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert triggers["customer_ordered"]["live_rules"] == ["nudge_ops_daily"]


@needs_fixture
@needs_platform
def test_editing_a_rule_through_the_ui_demotes_it(persistent) -> None:
    # The same safety rule as the CLI, reached through HTTP.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    persistent.post("/api/rules/thank_the_customer_weekly/mode", json={"mode": "live"})

    edited = DEFS.replace(
        "    policy: once_per_period\n    period: week", "    policy: once_ever", 1
    )
    persistent.post("/api/definitions/save", json={"yaml_text": edited})

    rules = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert rules["thank_the_customer_weekly"]["mode"] == "shadow"


@needs_fixture
@needs_platform
def test_editing_a_triggers_sql_demotes_every_rule_on_it(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    persistent.post("/api/rules/thank_the_customer_weekly/mode", json={"mode": "live"})
    persistent.post("/api/rules/nudge_ops_daily/mode", json={"mode": "live"})

    edited = DEFS.replace("WHERE o.status IN (4, 7)", "WHERE o.status IN (4, 7, 9)")
    persistent.post("/api/definitions/save", json={"yaml_text": edited})

    rules = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert rules["thank_the_customer_weekly"]["mode"] == "shadow"
    assert rules["nudge_ops_daily"]["mode"] == "shadow"


@needs_fixture
@needs_fixture
@needs_platform
def test_activity_reports_what_was_sent_not_a_mode(persistent) -> None:
    """A run carries no mode, because it cannot carry a true one.

    A poll fans out to every rule on the trigger and those have modes of their
    own. The single `mode` on a run was never set after creation, so Activity
    reported every run as shadow -- including ones where a live rule sent. The
    honest answer to "did anything leave" is a count of deliveries.
    """
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.get("/api/activity").json()
    assert "runs" in body
    for run in body["runs"]:
        assert "mode" not in run, "a run cannot have one mode; its rules each have one"
        assert run["sent"] >= 0


@needs_platform
def test_run_now_schedules_the_next_poll(persistent) -> None:
    # There was no way to make anything happen from the UI: you waited up to a
    # minute for the scheduled poll, or you edited trigger_state by hand.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    # Push it into the future first, the way release() does after a poll --
    # a freshly reconciled trigger is already due, so "sooner" would be
    # meaningless against it.
    import sqlalchemy
    from sqlalchemy import text as sa_text

    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "UPDATE trigger_state SET next_run_at = now() + interval '1 hour' "
                "WHERE trigger_name = 'customer_ordered'"
            )
        )
    engine.dispose()

    before = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert persistent.post("/api/triggers/customer_ordered/run").status_code == 200

    after = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert after["customer_ordered"]["next_run_at"] < before["customer_ordered"]["next_run_at"]
    assert after["customer_ordered"]["next_run_at"] <= datetime.now(UTC).isoformat()
    # The window is untouched: this asks for a poll, it does not widen one.
    assert after["customer_ordered"]["watermark"] == before["customer_ordered"]["watermark"]


@needs_fixture
@needs_platform
def test_rewinding_moves_the_watermark_back(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    before = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    response = persistent.post("/api/triggers/customer_ordered/rewind", json={"days": 30})
    assert response.status_code == 200, response.text

    after = {t["name"]: t for t in persistent.get("/api/triggers").json()["triggers"]}
    assert after["customer_ordered"]["watermark"] < before["customer_ordered"]["watermark"]


@needs_platform
def test_rewinding_an_unknown_trigger_is_a_404(persistent) -> None:
    assert persistent.post("/api/triggers/nope/rewind", json={"days": 1}).status_code == 404
    assert persistent.post("/api/triggers/nope/run").status_code == 404


@needs_platform
def test_a_nonsense_rewind_is_refused(persistent) -> None:
    assert (
        persistent.post("/api/triggers/customer_ordered/rewind", json={"days": 0}).status_code
        == 422
    )


@needs_platform
def test_unknown_rule_is_a_404(persistent) -> None:
    response = persistent.post("/api/rules/nope/mode", json={"mode": "live"})
    assert response.status_code == 404


@needs_fixture
@needs_platform
def test_halt_and_resume(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    persistent.post("/api/halt", json={"reason": "incident 42"})
    assert persistent.get("/api/rules").json()["halted"] is True
    assert persistent.get("/api/health").json()["halted"] == "incident 42"

    persistent.post("/api/resume")
    assert persistent.get("/api/rules").json()["halted"] is False


@needs_fixture
@needs_platform
def test_backtest_reads_definitions_from_the_control_plane(persistent) -> None:
    # No definitions file exists in this fixture at all -- proving the
    # backtest is served from the stored version, not from disk.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.post(
        "/api/backtest",
        json={"rule": "tell_ops_every_completed_order", "days": 120, "sample": 3},
    ).json()
    assert body["fires"] > 0
    assert body["null_event_time_rows"] == 40
    assert body["trigger"] == "order_completed"
    # Types come from the cursor, so a joined column is typed like any other.
    assert body["column_types"]["phone"] == "phone"


@needs_fixture
@needs_platform
def test_connection_survives_a_restart(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    # A brand new app object, as if the process had been restarted: no DSN in
    # memory, but the stored connection should be recovered transparently.
    fresh = api.create_app(session=api.Session())
    with TestClient(fresh, base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["connected"] is True
        assert client.get("/api/schema").status_code == 200


@needs_platform
def test_health_survives_a_broken_control_plane(persistent, monkeypatch) -> None:
    # Regression: health raised 500 when the schema and the code disagreed,
    # the UI swallowed the failure, and the page then rendered "nothing is
    # stored" -- a privacy claim produced by a request that never succeeded.
    from rowfire.platform import store as store_module

    def explode(*args, **kwargs):
        raise RuntimeError("column workspace.gone does not exist")

    monkeypatch.setattr(store_module, "ensure_workspace", explode)

    response = persistent.get("/api/health")
    assert response.status_code == 200, "health must answer when things are broken"

    body = response.json()
    assert body["degraded"] is not None
    assert "does not exist" in body["degraded"]


def test_a_negative_platform_probe_is_retried(monkeypatch) -> None:
    # Regression: a negative was cached for the life of the process, so a
    # control plane that was briefly unreachable at startup -- which compose
    # makes likely -- left the UI degraded forever with no way back.
    monkeypatch.delenv("ROWFIRE_PLATFORM_DSN", raising=False)
    api.reset_platform_cache()
    assert api.platform_ready() is False

    monkeypatch.setattr(api, "_NEGATIVE_TTL_SECONDS", 0.0)
    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv("ROWFIRE_MASTER_KEY", "x" * 44)

    from rowfire.platform import db

    db.reset_engine()
    try:
        assert api.platform_ready() is True, "must re-probe rather than stay negative"
    finally:
        api.reset_platform_cache()
        db.reset_engine()


# ------------------------------------------------------- composer surface
#
# These exist because the composer endpoints shipped once without them and a
# guessed column name (`action_id` for `action_id`) reached the
# screen as a 500.


@needs_fixture
@needs_platform
def test_schema_lists_tables_columns_and_clocks(persistent) -> None:
    # The reference material for someone writing a trigger's SQL. Served from
    # the live database, not from a model, because that is what the query will
    # actually run against.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})

    body = persistent.get("/api/schema").json()
    orders = next(t for t in body["tables"] if t["name"] == "orders")
    assert "completed_at" in orders["timestamps"]
    assert orders["primary_key"] == "id"
    assert {fk["target_table"] for fk in orders["foreign_keys"]} >= {"customers"}
    assert any(c["semantic_type"] == "money" for c in orders["columns"])


@needs_fixture
@needs_platform
def test_check_returns_the_output_columns_of_a_query(persistent) -> None:
    # The composer's core loop: write SQL, learn its shape, then pick the clock
    # and the key from the columns it really returns.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.post(
        "/api/triggers/check",
        json={
            "sql": "SELECT o.id, o.completed_at, c.first_name "
            "FROM orders o JOIN customers c ON c.id = o.customer_id",
            "event_time": "completed_at",
            "key": ["id"],
        },
    ).json()
    assert body["valid"] is True
    columns = {c["name"]: c["type"] for c in body["columns"]}
    assert columns["first_name"] == "string"  # from the join
    assert columns["completed_at"] == "timestamp"
    assert body["sql"].startswith("SELECT")


@needs_fixture
@needs_platform
def test_check_reports_columns_before_a_clock_is_chosen(persistent) -> None:
    # Otherwise the composer would have to guess the clock before it could
    # learn what the query returns.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.post(
        "/api/triggers/check", json={"sql": "SELECT id, completed_at FROM orders"}
    ).json()
    assert body["valid"] is False
    assert {c["name"] for c in body["columns"]} == {"id", "completed_at"}
    assert any("places a row in time" in e for e in body["errors"])


@needs_fixture
@needs_platform
@pytest.mark.parametrize(
    "sql,fragment",
    [
        ("SELECT id FROM orders; DROP TABLE orders", "one statement"),
        ("DELETE FROM orders", "DELETE"),
        ("SELECT statuss FROM orders", "statuss"),
        ("SELECT id FROM no_such_table", "no_such_table"),
    ],
)
def test_check_rejects_while_you_type(persistent, sql, fragment) -> None:
    # Same validator as everything else, reached before anything is saved --
    # and for the last two cases, the database's own answer.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.post(
        "/api/triggers/check",
        json={"sql": sql, "event_time": "completed_at", "key": ["id"]},
    ).json()
    assert body["valid"] is False
    assert any(fragment in error for error in body["errors"])


@needs_fixture
@needs_platform
def test_check_names_a_clock_that_is_not_an_output_column(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    body = persistent.post(
        "/api/triggers/check",
        json={"sql": "SELECT id FROM orders", "event_time": "completed_at", "key": ["id"]},
    ).json()
    assert body["valid"] is False
    assert any("completed_at" in e for e in body["errors"])


@needs_fixture
@needs_platform
def test_composer_writes_go_through_versioning(persistent) -> None:
    # The composer must not touch state rows directly, or versioning and
    # demote-on-edit are silently lost.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})
    persistent.post("/api/rules/tell_ops_every_completed_order/mode", json={"mode": "live"})

    response = persistent.put(
        "/api/triggers/order_completed",
        json={
            "name": "order_completed",
            "sql": "SELECT o.id, o.customer_id, o.completed_at, c.phone "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.status IN (4, 7)",
            "event_time": "completed_at",
            "key": ["id"],
        },
    )
    assert response.status_code == 200, response.text

    rules = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert rules["tell_ops_every_completed_order"]["mode"] == "shadow", (
        "editing the trigger must cost its rules their live status"
    )


@needs_fixture
@needs_platform
def test_a_composed_trigger_exports_as_readable_yaml(persistent) -> None:
    # `platform pull` is the only way definitions reach a file now, and the
    # usual reason to pull is version control. A multi-line query dumped as one
    # quoted line of \n escapes is valid YAML and a worthless diff.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    persistent.put(
        "/api/triggers/multiline",
        json={
            "name": "multiline",
            "sql": "SELECT id,\n       completed_at\nFROM orders\nWHERE status = 4",
            "event_time": "completed_at",
            "key": ["id"],
        },
    )

    exported = persistent.get("/api/definitions").json()["yaml_text"]
    assert "sql: |" in exported, "multi-line SQL should be a block scalar"
    assert "\\n" not in exported, "no escaped newlines in the export"
    # And it still parses back to the same query.
    reloaded = persistent.get("/api/definitions").json()
    trigger = next(t for t in reloaded["triggers"] if t["name"] == "multiline")
    assert trigger["sql"].splitlines()[0] == "SELECT id,"


@needs_fixture
@needs_platform
def test_create_and_delete_a_trigger(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    persistent.put(
        "/api/triggers/big_orders",
        json={
            "name": "big_orders",
            "sql": "SELECT id, total_amount, completed_at FROM orders WHERE total_amount > 500",
            "event_time": "completed_at",
            "key": ["id"],
        },
    )
    names = {t["name"] for t in persistent.get("/api/definitions").json()["triggers"]}
    assert "big_orders" in names

    assert persistent.delete("/api/triggers/big_orders").status_code == 200
    names = {t["name"] for t in persistent.get("/api/definitions").json()["triggers"]}
    assert "big_orders" not in names


@needs_fixture
@needs_platform
def test_deleting_a_trigger_that_still_feeds_rules_is_refused(persistent) -> None:
    # Letting it through would fail the cross-reference validator with an
    # "unknown trigger" message, which reads as the composer being broken.
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    response = persistent.delete("/api/triggers/customer_ordered")
    assert response.status_code == 409
    errors = response.json()["detail"]["errors"]
    assert any("thank_the_customer_weekly" in e for e in errors)


@needs_fixture
@needs_platform
def test_renaming_a_trigger_carries_its_rules(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    response = persistent.put(
        "/api/triggers/worker_activated",
        json={
            "name": "worker_went_active",
            "sql": "SELECT id, full_name, activated_at FROM workers WHERE status = 2",
            "event_time": "activated_at",
            "key": ["id"],
        },
    )
    assert response.status_code == 200, response.text

    body = persistent.get("/api/definitions").json()
    assert {t["name"] for t in body["triggers"]} >= {"worker_went_active"}
    rule = next(r for r in body["rules"] if r["name"] == "welcome_new_worker")
    assert rule["trigger"] == "worker_went_active"


@needs_fixture
@needs_platform
def test_create_and_delete_a_rule(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    response = persistent.put(
        "/api/rules/monthly_recap",
        json={
            "name": "monthly_recap",
            "trigger": "customer_ordered",
            "description": "One message per customer per month",
            "policy": "once_per_period",
            "period": "month",
        },
    )
    assert response.status_code == 200, response.text

    rules = {r["name"]: r for r in persistent.get("/api/definitions").json()["rules"]}
    assert rules["monthly_recap"]["period"] == "month"
    # And it is immediately schedulable, on the trigger it was pointed at.
    live = {r["name"]: r for r in persistent.get("/api/rules").json()["rules"]}
    assert live["monthly_recap"]["trigger"] == "customer_ordered"

    assert persistent.delete("/api/rules/monthly_recap").status_code == 200
    rules = {r["name"] for r in persistent.get("/api/definitions").json()["rules"]}
    assert "monthly_recap" not in rules


# The name constraint, and the error the UI has to render.
#
# These names are identifiers: YAML keys, what a rule's `trigger` points at,
# and what the fire ledger is keyed on. The constraint is right; the 422 it
# produced was reaching the screen as a bare "Unprocessable Entity", because
# FastAPI's own request-validation errors have a different shape from ours and
# the front end only knew about ours.

NAME_PATTERN = "^[a-z][a-z0-9_]*$"


@needs_platform
def test_an_invalid_name_explains_itself_in_the_shape_the_ui_parses(persistent) -> None:
    response = persistent.put(
        "/api/rules/x",
        json={"name": "thank the customer", "trigger": "order_completed", "policy": "once_ever"},
    )
    assert response.status_code == 422

    # A list of {loc, msg, ctx}, not our {"errors": [...]}. ui/src/api.ts
    # branches on exactly this; if FastAPI ever changes it, the front end goes
    # back to showing a bare status line and this is what catches it.
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    entry = detail[0]
    assert entry["loc"][-1] == "name"
    assert entry["ctx"]["pattern"] == NAME_PATTERN


@needs_platform
def test_the_name_pattern_is_the_one_the_front_end_hardcodes(persistent) -> None:
    # Duplicated across the language boundary, so it needs a tripwire: drift
    # either blocks names the API would accept or lets through ones it will
    # not, and both are discovered by a user rather than by a test.
    from rowfire.api import RuleDraft, TriggerDraft

    for model in (RuleDraft, TriggerDraft):
        # metadata carries several constraints (min_length among them), so the
        # pattern is looked up rather than indexed into positionally.
        patterns = [getattr(item, "pattern", None) for item in model.model_fields["name"].metadata]
        assert NAME_PATTERN in patterns, f"{model.__name__} lost its name pattern"

    ui_source = (REPO_ROOT / "ui" / "src" / "api.ts").read_text()
    assert f"export const NAME_PATTERN = /{NAME_PATTERN}/;" in ui_source


@needs_fixture
@needs_platform
def test_a_rule_pointing_at_no_trigger_is_rejected(persistent) -> None:
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    response = persistent.put(
        "/api/rules/orphan",
        json={"name": "orphan", "trigger": "does_not_exist", "policy": "once_ever"},
    )
    assert response.status_code == 422
    assert any("does_not_exist" in e for e in response.json()["detail"]["errors"])


# ------------------------------------------------- integrations and actions
#
# The two entities someone configures by hand. These endpoints are the whole
# claim that a new system can be added without writing Python, so the custom
# path is tested end to end rather than only the catalogue one.


@needs_platform
def test_catalogue_declares_what_each_system_needs(persistent) -> None:
    body = persistent.get("/api/catalogue").json()
    entries = {e["name"]: e for e in body["catalogue"]}

    slack = entries["slack"]
    assert slack["auth_credential"] == "bot_token"
    token = next(c for c in slack["credentials"] if c["key"] == "bot_token")
    assert token["secret"] is True
    assert "xoxb-" in (token["help"] or "")

    # Braze proves the shape is not Slack's: two credentials, one of which is
    # configuration rather than a secret.
    braze = entries["braze"]
    kinds = {c["key"]: c["secret"] for c in braze["credentials"]}
    assert kinds == {"api_key": True, "app_id": False}


@needs_platform
def test_create_an_integration_from_the_catalogue(persistent) -> None:
    response = persistent.post(
        "/api/integrations",
        json={
            "name": "acme-slack",
            "provider": "slack",
            "credentials": {"bot_token": "xoxb-secret"},
        },
    )
    assert response.status_code == 200, response.text

    listed = {i["name"]: i for i in persistent.get("/api/integrations").json()["integrations"]}
    slack = listed["acme-slack"]
    assert slack["provider"] == "slack"
    assert slack["base_url"] == "https://slack.com/api"
    # The template's actions come with it.
    assert [a["name"] for a in slack["actions"]] == ["send_message"]
    # Declared, so the rule screen can ask for each one properly rather than
    # showing a text box named after a placeholder.
    declared = {p["name"]: p for p in slack["actions"][0]["parameters"]}
    assert set(declared) == {"channel", "text"}
    assert declared["channel"]["label"] == "Channel"
    assert declared["channel"]["type"] == "string"
    assert "#ops" in (declared["channel"]["help"] or "")


@needs_platform
def test_credentials_are_never_returned_to_the_browser(persistent) -> None:
    persistent.post(
        "/api/integrations",
        json={
            "name": "acme-slack",
            "provider": "slack",
            "credentials": {"bot_token": "xoxb-super-secret"},
        },
    )
    body = persistent.get("/api/integrations").text
    assert "xoxb-super-secret" not in body
    # ...but the UI can still say which credentials are set.
    listed = persistent.get("/api/integrations").json()["integrations"][0]
    assert listed["credential_keys"] == ["bot_token"]


@needs_platform
def test_two_integrations_can_share_a_provider(persistent) -> None:
    for name, token in (("acme-slack", "xoxb-a"), ("support-slack", "xoxb-b")):
        assert (
            persistent.post(
                "/api/integrations",
                json={"name": name, "provider": "slack", "credentials": {"bot_token": token}},
            ).status_code
            == 200
        )

    listed = {i["name"]: i for i in persistent.get("/api/integrations").json()["integrations"]}
    assert set(listed) == {"acme-slack", "support-slack"}
    assert listed["acme-slack"]["id"] != listed["support-slack"]["id"]


@needs_platform
def test_define_a_custom_integration_and_action_without_code(persistent) -> None:
    """The open-ended path, which is the point of the whole design.

    No catalogue entry, no Python: a base URL, a header credential, and one
    REST call described as data.
    """
    created = persistent.post(
        "/api/integrations",
        json={
            "name": "our-crm",
            "description": "The in-house CRM",
            "base_url": "https://api.example.com/v2",
            "auth_kind": "header",
            "auth_header_name": "X-API-Key",
            "auth_credential": "api_key",
            "credentials": {"api_key": "k-123"},
        },
    )
    assert created.status_code == 200, created.text
    integration_id = created.json()["id"]

    saved = persistent.put(
        f"/api/integrations/{integration_id}/actions/update_tier",
        json={
            "description": "Set a contact's tier",
            "method": "PATCH",
            "path": "/contacts/{{ contact_id }}",
            "body": {"fields": {"{{ field }}": "{{ value }}"}},
        },
    )
    assert saved.status_code == 200, saved.text
    action = saved.json()["action"]
    assert action["method"] == "PATCH"
    # The placeholder in the body's *key* is discovered, so a binding that
    # omits it is refused at save time rather than at 3am.
    assert sorted(action["parameter_names"]) == ["contact_id", "field", "value"]

    listed = {i["name"]: i for i in persistent.get("/api/integrations").json()["integrations"]}
    assert listed["our-crm"]["provider"] == "custom"
    assert [a["name"] for a in listed["our-crm"]["actions"]] == ["update_tier"]


@needs_platform
def test_an_action_can_be_edited_and_deleted(persistent) -> None:
    created = persistent.post(
        "/api/integrations",
        json={"name": "our-crm", "base_url": "https://api.example.com"},
    )
    integration_id = created.json()["id"]

    persistent.put(
        f"/api/integrations/{integration_id}/actions/ping",
        json={"method": "GET", "path": "/ping"},
    )
    edited = persistent.put(
        f"/api/integrations/{integration_id}/actions/ping",
        json={"method": "GET", "path": "/healthz"},
    )
    assert edited.json()["action"]["path"] == "/healthz"

    listed = persistent.get("/api/integrations").json()["integrations"][0]
    assert len(listed["actions"]) == 1, "editing must replace, not duplicate"

    assert persistent.delete(f"/api/integrations/{integration_id}/actions/ping").status_code == 200
    listed = persistent.get("/api/integrations").json()["integrations"][0]
    assert listed["actions"] == []


@needs_platform
def test_an_unknown_method_is_refused(persistent) -> None:
    created = persistent.post(
        "/api/integrations", json={"name": "our-crm", "base_url": "https://api.example.com"}
    )
    response = persistent.put(
        f"/api/integrations/{created.json()['id']}/actions/weird",
        json={"method": "TRACE", "path": "/x"},
    )
    assert response.status_code == 422


@needs_platform
def test_deleting_an_integration_takes_its_actions_with_it(persistent) -> None:
    created = persistent.post(
        "/api/integrations",
        json={"name": "slack", "provider": "slack", "credentials": {"bot_token": "xoxb"}},
    )
    integration_id = created.json()["id"]
    assert persistent.delete(f"/api/integrations/{integration_id}").status_code == 200
    assert persistent.get("/api/integrations").json()["integrations"] == []


# The fixture database's owner, which can write. Only the simulated-activity
# tests use it, and only on a temporary table.
FIXTURE_ADMIN_DSN = os.environ.get(
    "TEST_FIXTURE_ADMIN_URL",
    "postgresql://rowfire:rowfire@localhost:5433/rowfire_fixture",
)


@needs_platform
def test_simulated_activity_does_not_exist_unless_configured(persistent, monkeypatch) -> None:
    # A real install has no write path into anything it reads: no button, and
    # the endpoint answers like a route that is not there.
    monkeypatch.delenv("ROWFIRE_DEMO_ACTIVITY_DSN", raising=False)
    monkeypatch.delenv("ROWFIRE_DEMO_ACTIVITY_SQL", raising=False)

    assert persistent.get("/api/health").json()["demo_activity"] is False
    assert persistent.post("/api/demo/activity").status_code == 404


@needs_fixture
@needs_platform
def test_simulated_activity_runs_its_file_and_polls_everything(
    persistent, monkeypatch, tmp_path
) -> None:
    script = tmp_path / "activity.sql"
    # Several statements, as the real file has, on a table that vanishes with
    # the session -- the fixture's own rows are asserted by other tests.
    script.write_text(
        "CREATE TEMP TABLE burst (n int);\n"
        "INSERT INTO burst VALUES (1);\n"
        "INSERT INTO burst SELECT n + 1 FROM burst;\n"
    )
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_DSN", FIXTURE_ADMIN_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_SQL", str(script))
    persistent.post("/api/connect", json={"dsn": FIXTURE_DSN})
    persistent.post("/api/definitions/save", json={"yaml_text": DEFS})

    assert persistent.get("/api/health").json()["demo_activity"] is True
    response = persistent.post("/api/demo/activity")

    assert response.status_code == 200, response.text
    triggers = {t["name"] for t in persistent.get("/api/triggers").json()["triggers"]}
    assert set(response.json()["queued"]) == triggers


@needs_fixture
@needs_platform
def test_a_failing_activity_script_says_why_without_the_dsn(
    persistent, monkeypatch, tmp_path
) -> None:
    script = tmp_path / "activity.sql"
    script.write_text("INSERT INTO no_such_table VALUES (1);")
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_DSN", FIXTURE_ADMIN_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_SQL", str(script))

    response = persistent.post("/api/demo/activity")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "no_such_table" in detail
    assert "rowfire:rowfire" not in detail


@needs_platform
def test_a_binding_names_an_integration_and_an_action(persistent) -> None:
    persistent.post(
        "/api/integrations",
        json={"name": "acme-slack", "provider": "slack", "credentials": {"bot_token": "x"}},
    )
    created = persistent.post(
        "/api/bindings",
        json={
            "rule_name": "tell_ops",
            "integration": "acme-slack",
            "action": "send_message",
            "parameters": {"channel": "#ops", "text": "order {{ id }}"},
        },
    )
    assert created.status_code == 200, created.text

    listed = persistent.get("/api/bindings").json()["bindings"][0]
    assert listed["integration"] == "acme-slack"
    assert listed["action"] == "send_message"


@needs_platform
def test_the_inbox_shows_what_rules_posted_to_it(persistent) -> None:
    """The delivery log is the inbox: shadow and live, newest first.

    Deliveries to a real integration stay out of it, and each item says
    whether it is a message or a ticket so the page can draw it as one.
    """
    from rowfire.platform import actions, store
    from rowfire.platform import db as platform_db

    assert persistent.get("/api/inbox").json() == {"installed": [], "items": []}

    persistent.post("/api/integrations", json={"name": "Demo inbox", "provider": "demo_inbox"})
    persistent.post(
        "/api/integrations",
        json={"name": "acme-slack", "provider": "slack", "credentials": {"bot_token": "x"}},
    )
    for integration, action, parameters in [
        ("Demo inbox", "post_message", {"channel": "#ops", "text": "order {{ id }}"}),
        (
            "Demo inbox",
            "create_ticket",
            {"subject": "Order {{ id }}", "body": "b", "requester_email": "a@b.example"},
        ),
        ("acme-slack", "send_message", {"channel": "#ops", "text": "order {{ id }}"}),
    ]:
        created = persistent.post(
            "/api/bindings",
            json={
                "rule_name": "tell_ops",
                "integration": integration,
                "action": action,
                "parameters": parameters,
            },
        )
        assert created.status_code == 200, created.text

    with platform_db.session_scope() as session:
        workspace = store.ensure_workspace(session)
        actions.deliver(
            session,
            workspace=workspace,
            rule_name="tell_ops",
            mode=Mode.shadow,
            row={"id": 7},
            fire_id=None,
        )

    body = persistent.get("/api/inbox").json()
    assert body["installed"] == ["Demo inbox"]
    assert {item["kind"] for item in body["items"]} == {"message", "ticket"}
    message = next(item for item in body["items"] if item["kind"] == "message")
    assert message["body"] == {"channel": "#ops", "text": "order 7"}
    assert message["mode"] == "shadow" and message["status"] == "suppressed"
    assert message["rule"] == "tell_ops" and message["integration"] == "Demo inbox"


@needs_platform
def test_a_binding_can_cap_per_recipient(persistent) -> None:
    """The cap's own lever, which used to be unreachable.

    `bind()` has always taken a recipient template, but no caller could pass
    one -- so every delivery for an integration counted against one shared
    bucket. A rule thanking 121 customers sent 10 of them and suppressed the
    rest, and nothing in the UI could change that.
    """
    persistent.post(
        "/api/integrations",
        json={"name": "acme-slack", "provider": "slack", "credentials": {"bot_token": "x"}},
    )
    created = persistent.post(
        "/api/bindings",
        json={
            "rule_name": "thank_the_customer",
            "integration": "acme-slack",
            "action": "send_message",
            "parameters": {"channel": "#ops", "text": "hi"},
            "recipient_template": "{{ customer_id }}",
        },
    )
    assert created.status_code == 200, created.text
    listed = persistent.get("/api/bindings").json()["bindings"][0]
    assert listed["recipient_template"] == "{{ customer_id }}"


@needs_platform
def test_the_frequency_cap_can_be_read_and_changed(persistent) -> None:
    # A safety net nobody can see reads as the product silently dropping
    # messages, which is how it looked before this endpoint existed.
    defaults = persistent.get("/api/limits").json()
    assert defaults["cap_per_recipient"] == 10
    assert defaults["cap_window_hours"] == 24

    updated = persistent.post("/api/limits", json={"cap_per_recipient": 500, "cap_window_hours": 1})
    assert updated.status_code == 200
    assert persistent.get("/api/limits").json()["cap_per_recipient"] == 500


@needs_platform
def test_a_nonsense_cap_is_refused(persistent) -> None:
    assert (
        persistent.post(
            "/api/limits", json={"cap_per_recipient": 0, "cap_window_hours": 24}
        ).status_code
        == 422
    )


@needs_platform
def test_a_binding_missing_a_parameter_is_refused(persistent) -> None:
    persistent.post(
        "/api/integrations",
        json={"name": "acme-slack", "provider": "slack", "credentials": {"bot_token": "x"}},
    )
    response = persistent.post(
        "/api/bindings",
        json={
            "rule_name": "tell_ops",
            "integration": "acme-slack",
            "action": "send_message",
            "parameters": {"channel": "#ops"},
        },
    )
    assert response.status_code == 422
    assert any("text" in e for e in response.json()["detail"]["errors"])


@needs_platform
def test_integrations_and_bindings_endpoints_answer(persistent) -> None:
    # A smoke test, but the one that would have caught the 500.
    assert persistent.get("/api/integrations").status_code == 200
    assert persistent.get("/api/bindings").json() == {"bindings": []}
