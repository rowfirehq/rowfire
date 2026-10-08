"""Scheduler, trigger/rule state, and delivery.

Requires the control-plane Postgres. These exercise the safety rules from the
concept doc as behaviour rather than as comments: cold-start watermark, demote
on edit, the kill switch, and the frequency cap.

The split between trigger and rule shows up throughout. Scheduling is per
trigger -- one query, leased once, run once. Mode is per rule, so one
automation on a query can be live while another on the same query is still
being watched.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlmodel import Session, select

from rowfire.definitions import loads as loads_definitions
from rowfire.platform import actions, crypto, integrations, scheduler, store
from rowfire.platform.models import DeliveryStatus, Mode, RuleState, TriggerState

PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)

DEFS = """
version: 2
source: { dsn_env: DATABASE_URL }
triggers:
  order_completed:
    sql: SELECT id, completed_at FROM orders WHERE status = 4
    event_time: completed_at
    key: [id]
rules:
  tell_ops:
    trigger: order_completed
    policy: once_ever
"""

# The same trigger with a second rule on it, for the fan-out cases.
TWO_RULES = (
    DEFS
    + """
  thank_the_customer:
    trigger: order_completed
    policy: once_ever
"""
)


def _available() -> bool:
    try:
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM trigger_state LIMIT 1"))
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
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


def key() -> bytes:
    return base64.urlsafe_b64decode(crypto.generate_master_key())


# ----------------------------------------------------- trigger/rule state


def test_a_new_rule_starts_in_shadow(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()
    rule = session.exec(select(RuleState)).one()
    assert rule.mode is Mode.shadow


def test_a_new_triggers_watermark_starts_at_now(session) -> None:
    # Connecting to 400,000 historical completed orders must not fire 400,000
    # events. The watermark defaults to now, never to the beginning of time.
    before = datetime.now(UTC)
    store.save_definitions(session, DEFS)
    session.commit()

    state = session.exec(select(TriggerState)).one()
    assert state.watermark is not None and state.watermark >= before - timedelta(seconds=5)


def test_two_rules_on_one_trigger_share_one_schedule(session) -> None:
    # Scheduling is per trigger: two rules must not mean two polls of the same
    # query against the customer's replica.
    store.save_definitions(session, TWO_RULES)
    session.commit()
    assert len(session.exec(select(TriggerState)).all()) == 1
    assert len(session.exec(select(RuleState)).all()) == 2


def test_editing_a_rule_demotes_it(session) -> None:
    store.save_definitions(session, DEFS)
    store.set_mode(session, "tell_ops", Mode.live)
    session.commit()

    edited = DEFS.replace("policy: once_ever", "policy: once_per_period\n    period: day")
    store.save_definitions(session, edited)
    session.commit()

    rule = session.exec(select(RuleState)).one()
    assert rule.mode is Mode.shadow, "an edit must cost live status"


def test_editing_a_trigger_demotes_every_rule_on_it(session) -> None:
    """The reason rule_checksum folds in the trigger it reads.

    Changing a trigger's SQL changes what every rule on it will fire for. A
    checksum over the rule alone would leave those rules live against a query
    nobody has watched.
    """
    store.save_definitions(session, TWO_RULES)
    store.set_mode(session, "tell_ops", Mode.live)
    store.set_mode(session, "thank_the_customer", Mode.live)
    session.commit()

    edited = TWO_RULES.replace("WHERE status = 4", "WHERE status IN (4, 7)")
    store.save_definitions(session, edited)
    session.commit()

    modes = {r.rule_name: r.mode for r in session.exec(select(RuleState)).all()}
    assert modes == {"tell_ops": Mode.shadow, "thank_the_customer": Mode.shadow}


def test_unrelated_edits_do_not_demote(session) -> None:
    # A global file checksum would demote everything whenever anyone touched
    # anything, and people would learn to ignore the demotion.
    store.save_definitions(session, TWO_RULES)
    store.set_mode(session, "tell_ops", Mode.live)
    session.commit()

    cosmetic = TWO_RULES.replace("version: 2", "version: 2\n# a comment")
    store.save_definitions(session, cosmetic)
    session.commit()

    modes = {r.rule_name: r.mode for r in session.exec(select(RuleState)).all()}
    assert modes["tell_ops"] is Mode.live


def test_editing_one_rule_leaves_its_sibling_alone(session) -> None:
    store.save_definitions(session, TWO_RULES)
    store.set_mode(session, "tell_ops", Mode.live)
    store.set_mode(session, "thank_the_customer", Mode.live)
    session.commit()

    edited = TWO_RULES.replace(
        "  thank_the_customer:\n    trigger: order_completed\n    policy: once_ever",
        "  thank_the_customer:\n    trigger: order_completed\n"
        "    policy: once_per_period\n    period: week",
    )
    store.save_definitions(session, edited)
    session.commit()

    modes = {r.rule_name: r.mode for r in session.exec(select(RuleState)).all()}
    assert modes["thank_the_customer"] is Mode.shadow
    assert modes["tell_ops"] is Mode.live


def test_removing_a_trigger_disables_its_state(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()
    renamed = DEFS.replace("  order_completed:", "  renamed:").replace(
        "trigger: order_completed", "trigger: renamed"
    )
    store.save_definitions(session, renamed)
    session.commit()

    states = {s.trigger_name: s for s in session.exec(select(TriggerState)).all()}
    assert states["order_completed"].enabled is False
    assert states["renamed"].enabled is True


def test_a_re_created_rule_comes_back_enabled(session) -> None:
    """Delete a rule, add it again, and it must actually run.

    Reconcile re-enabled a returning trigger but not a returning rule, so a
    rule brought back under the same name stayed disabled: present in the
    definitions, listed in the UI alongside everything else, and silently
    dropped from every poll. Nothing said so except a small tag.
    """
    store.save_definitions(session, TWO_RULES)
    session.commit()

    store.save_definitions(session, DEFS)  # thank_the_customer removed
    session.commit()
    rules = {r.rule_name: r for r in session.exec(select(RuleState)).all()}
    assert rules["thank_the_customer"].enabled is False

    store.save_definitions(session, TWO_RULES)  # and back again
    session.commit()
    rules = {r.rule_name: r for r in session.exec(select(RuleState)).all()}
    assert rules["thank_the_customer"].enabled is True
    assert rules["thank_the_customer"].mode is Mode.shadow


def test_a_re_created_rule_does_not_return_live(session) -> None:
    # It left as live, but nobody has watched what it does since. Coming back
    # straight to live would send on the first poll after a round trip that
    # looks like an edit.
    store.save_definitions(session, TWO_RULES)
    store.set_mode(session, "thank_the_customer", Mode.live)
    session.commit()

    store.save_definitions(session, DEFS)
    session.commit()
    store.save_definitions(session, TWO_RULES)
    session.commit()

    rules = {r.rule_name: r for r in session.exec(select(RuleState)).all()}
    assert rules["thank_the_customer"].mode is Mode.shadow


def test_a_disabled_rule_is_excluded_from_the_fan_out(session) -> None:
    # The consequence the bug above had: the poll silently skips it.
    store.save_definitions(session, TWO_RULES)
    session.commit()
    rules = {r.rule_name: r for r in session.exec(select(RuleState)).all()}
    rules["thank_the_customer"].enabled = False
    session.add(rules["thank_the_customer"])
    session.commit()

    state = session.exec(select(TriggerState)).one()
    outcome = scheduler.poll(
        session, state, loads_definitions(TWO_RULES), "postgresql://unused/none"
    )
    assert "thank_the_customer" not in {r.rule_name for r in outcome.rules}


def test_removing_a_rule_disables_its_state(session) -> None:
    store.save_definitions(session, TWO_RULES)
    session.commit()
    store.save_definitions(session, DEFS)
    session.commit()

    rules = {r.rule_name: r for r in session.exec(select(RuleState)).all()}
    assert rules["thank_the_customer"].enabled is False
    assert rules["tell_ops"].enabled is True


# ------------------------------------------------------------- credentials


def test_connection_roundtrips_through_encryption(session) -> None:
    master = key()
    dsn = "postgresql://readonly:secret@db:5432/prod"
    store.save_connection(session, dsn, master_key=master)
    session.commit()

    stored = store.get_connection(session)
    assert stored is not None
    assert store.reveal_dsn(stored, master_key=master) == dsn
    # And the plaintext is genuinely not in the row.
    assert b"secret" not in stored.dsn_ciphertext


# ---------------------------------------------------------------- leasing


def test_lease_returns_due_triggers_then_nothing(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()

    first = scheduler.lease_next(session)
    assert first is not None
    # Already leased, so a second worker gets nothing.
    assert scheduler.lease_next(session) is None


def test_two_workers_never_lease_the_same_trigger(engine) -> None:
    with Session(engine) as session:
        store.save_definitions(session, DEFS)
        session.commit()

    leased = []
    for _ in range(2):
        with Session(engine) as session:
            state = scheduler.lease_next(session, worker="w")
            leased.append(state.trigger_name if state else None)

    assert leased.count("order_completed") == 1
    assert leased.count(None) == 1


def test_expired_lease_is_reclaimed(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()
    scheduler.lease_next(session)

    # A worker that died leaves a lease behind; it must not block forever.
    session.execute(text("UPDATE trigger_state SET locked_until = now() - interval '1 hour'"))
    session.commit()
    assert scheduler.lease_next(session) is not None


def test_release_schedules_the_next_run(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()
    state = scheduler.lease_next(session)
    moment = datetime.now(UTC)

    scheduler.release(session, state, now=moment, advance_watermark_to=moment)
    refreshed = session.exec(select(TriggerState)).one()
    assert refreshed.locked_until is None
    assert refreshed.next_run_at is not None and refreshed.next_run_at > moment
    assert refreshed.watermark == moment


# -------------------------------------------------------------- delivery


class Recorder:
    """A transport that records calls instead of making them."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.status = status
        self.payload = payload if payload is not None else {"ok": True, "ts": "1700000000.1"}

    def __call__(self, method, url, headers, body, timeout_ms):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.payload


def _bound(
    session,
    *,
    rule_name="tell_ops",
    action="send_message",
    parameters=None,
    secret="xoxb-secret",
    base_url=None,
    defs=DEFS,
):
    """A workspace with one rule bound to one integration action."""
    store.save_definitions(session, defs)
    session.commit()
    workspace = store.ensure_workspace(session)
    master = key()
    integrations.install(
        session,
        integrations.load_template("slack"),
        name="slack",
        credentials={"bot_token": secret},
        base_url=base_url,
        master_key=master,
    )
    integrations.bind(
        session,
        rule_name,
        "slack",
        action,
        parameters or {"channel": "#ops", "text": "order {{ id }} completed"},
    )
    session.commit()
    return workspace, master


def _deliver(session, workspace, master, *, mode, row, rule_name="tell_ops", transport=None):
    return actions.deliver(
        session,
        workspace=workspace,
        rule_name=rule_name,
        mode=mode,
        row=row,
        fire_id=None,
        master_key=master,
        transport=transport,
    )


def test_no_bindings_means_no_deliveries(session) -> None:
    store.save_definitions(session, DEFS)
    session.commit()
    workspace = store.ensure_workspace(session)

    assert _deliver(session, workspace, None, mode=Mode.shadow, row={"id": 1}) == []


def test_bindings_are_per_rule_not_per_trigger(session) -> None:
    # Two rules on one trigger, one binding each. A binding keyed on the
    # trigger would make both rules send both messages.
    workspace, master = _bound(session, defs=TWO_RULES)
    integrations.bind(
        session,
        "thank_the_customer",
        "slack",
        "send_message",
        {"channel": "#customers", "text": "thanks for order {{ id }}"},
    )
    session.commit()

    [ops] = _deliver(session, workspace, master, mode=Mode.shadow, row={"id": 7})
    [thanks] = _deliver(
        session,
        workspace,
        master,
        mode=Mode.shadow,
        row={"id": 7},
        rule_name="thank_the_customer",
    )
    assert ops.rendered["body"]["channel"] == "#ops"
    assert thanks.rendered["body"]["channel"] == "#customers"


def test_shadow_renders_but_does_not_send(session) -> None:
    workspace, master = _bound(session)
    recorder = Recorder()

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.shadow, row={"id": 42}, transport=recorder
    )

    assert recorder.calls == [], "shadow must not send"
    # ...but the request is fully built, so promotion is not a leap into an
    # untested path.
    assert delivery.rendered["body"]["text"] == "order 42 completed"
    assert delivery.suppressed_reason and "shadow" in delivery.suppressed_reason


def test_live_sends_through_the_integration(session) -> None:
    workspace, master = _bound(session)
    recorder = Recorder()

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 42}, transport=recorder
    )

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == "https://slack.com/api/chat.postMessage"
    assert recorder.calls[0]["body"] == {"channel": "#ops", "text": "order 42 completed"}
    assert delivery.status is DeliveryStatus.sent


def test_a_live_rule_delivers_to_the_demo_inbox_without_sending(session) -> None:
    # Promoting a rule that points at the inbox is the demo's "it worked"
    # moment, so it has to end as `sent` -- with nothing leaving the process.
    store.save_definitions(session, DEFS)
    session.commit()
    workspace = store.ensure_workspace(session)
    integrations.install(session, integrations.load_template("demo_inbox"), name="Demo inbox")
    integrations.bind(
        session,
        "tell_ops",
        "Demo inbox",
        "post_message",
        {"channel": "#ops", "text": "order {{ id }} completed"},
    )
    session.commit()
    recorder = Recorder()

    [delivery] = _deliver(
        session, workspace, None, mode=Mode.live, row={"id": 42}, transport=recorder
    )

    assert recorder.calls == []
    assert delivery.status is DeliveryStatus.sent
    assert delivery.request_url == "inbox://demo/messages"
    assert delivery.rendered["body"] == {"channel": "#ops", "text": "order 42 completed"}


def test_the_token_is_sent_but_never_stored(session) -> None:
    # delivery.rendered is displayed in the UI. An auth header rendered into
    # it would be a credential written to the database in plaintext.
    workspace, master = _bound(session, secret="xoxb-super-secret")
    recorder = Recorder()

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 1}, transport=recorder
    )

    sent_headers = {k.lower(): v for k, v in recorder.calls[0]["headers"].items()}
    assert sent_headers["authorization"] == "Bearer xoxb-super-secret"
    assert "xoxb-super-secret" not in json.dumps(delivery.rendered)
    assert not any("authorization" in k.lower() for k in delivery.rendered["headers"])


def test_a_template_referring_to_a_missing_column_fails_clearly(session) -> None:
    workspace, master = _bound(session, parameters={"channel": "#ops", "text": "{{ nonexistent }}"})
    recorder = Recorder()

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 1}, transport=recorder
    )

    assert recorder.calls == []
    assert delivery.status is DeliveryStatus.failed
    assert "nonexistent" in (delivery.error or "")


def test_a_joined_column_is_available_to_a_template(session) -> None:
    # Enrichment for free: a column the trigger's join produced is just a
    # column, with no separate step to configure.
    workspace, master = _bound(
        session, parameters={"channel": "#ops", "text": "hi {{ first_name }}"}
    )
    recorder = Recorder()

    [delivery] = _deliver(
        session,
        workspace,
        master,
        mode=Mode.live,
        row={"id": 1, "first_name": "Mona"},
        transport=recorder,
    )
    assert recorder.calls[0]["body"]["text"] == "hi Mona"
    assert delivery.status is DeliveryStatus.sent


def test_an_api_error_is_recorded_not_raised(session) -> None:
    workspace, master = _bound(session)
    recorder = Recorder(status=429, payload={"error": "rate_limited"})

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 1}, transport=recorder
    )
    assert delivery.status is DeliveryStatus.failed
    assert "429" in (delivery.error or "")


def test_slack_reporting_failure_in_a_200_is_caught(session) -> None:
    # Slack answers 200 with {"ok": false}. Treating that as success would
    # record a send that never happened.
    workspace, master = _bound(session)
    recorder = Recorder(payload={"ok": False, "error": "channel_not_found"})

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 1}, transport=recorder
    )
    assert delivery.status is DeliveryStatus.failed
    assert "channel_not_found" in (delivery.error or "")


def test_frequency_cap_suppresses_beyond_the_limit(session) -> None:
    workspace, master = _bound(session)
    workspace.cap_per_recipient = 3
    session.flush()
    recorder = Recorder()

    statuses = [
        _deliver(session, workspace, master, mode=Mode.live, row={"id": i}, transport=recorder)[
            0
        ].status
        for i in range(6)
    ]
    assert statuses.count(DeliveryStatus.sent) == 3
    assert statuses.count(DeliveryStatus.suppressed) == 3
    assert len(recorder.calls) == 3


def test_shadow_does_not_spend_the_live_budget(session) -> None:
    # Regression: budgets were shared, so watching a rule in shadow burned the
    # real allowance and promotion found its recipients capped out.
    workspace, master = _bound(session)
    workspace.cap_per_recipient = 2
    session.flush()
    recorder = Recorder()

    for i in range(5):  # exhaust the shadow budget
        _deliver(session, workspace, master, mode=Mode.shadow, row={"id": i}, transport=recorder)

    [delivery] = _deliver(
        session, workspace, master, mode=Mode.live, row={"id": 99}, transport=recorder
    )
    assert delivery.status is DeliveryStatus.sent, "promotion must start with a full budget"


# ------------------------------------------------------------------ poll


def test_kill_switch_stops_polling(session) -> None:
    store.save_definitions(session, DEFS)
    store.halt(session, "incident 123")
    session.commit()

    state = session.exec(select(TriggerState)).one()
    definitions = loads_definitions(DEFS)
    outcome = scheduler.poll(session, state, definitions, "postgresql://unused/none")

    # Never even opened a connection to the customer's database.
    assert outcome.skipped and "halted" in outcome.skipped
    assert outcome.matched_rows == 0


def test_a_trigger_without_event_time_is_refused_for_live_polling(session) -> None:
    timeless = DEFS.replace("    event_time: completed_at\n", "")
    store.save_definitions(session, timeless)
    session.commit()
    state = session.exec(select(TriggerState)).one()

    outcome = scheduler.poll(
        session, state, loads_definitions(timeless), "postgresql://unused/none"
    )
    # Without a time column every poll would re-scan the whole table.
    assert outcome.error and "event_time" in outcome.error


def test_a_trigger_with_no_rules_is_not_polled(session) -> None:
    # Nothing to fan out to, so running the query would be work for nothing.
    no_rules = DEFS.replace(
        "rules:\n  tell_ops:\n    trigger: order_completed\n    policy: once_ever", "rules: {}"
    )
    store.save_definitions(session, no_rules)
    session.commit()
    state = session.exec(select(TriggerState)).one()

    outcome = scheduler.poll(
        session, state, loads_definitions(no_rules), "postgresql://unused/none"
    )
    assert outcome.skipped and "rule" in outcome.skipped


# ------------------------------------------------------------ workspaces

FIXTURE_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture",
)


def test_a_worker_serving_every_workspace_delivers_with_each_ones_own_bindings(
    session,
) -> None:
    """Two workspaces, the same rule name, different bindings.

    A worker started with no workspace leases whichever trigger is due, in
    any workspace, and the poll acts on the leased trigger's workspace end to
    end. Delivery used to be called without it, so a fire in any workspace
    but the default would have looked up the default's bindings.
    """
    import uuid

    from rowfire.platform.models import Delivery

    master = key()
    channels = {uuid.uuid4(): "#alpha", uuid.uuid4(): "#beta"}
    for workspace_id, channel in channels.items():
        store.save_definitions(session, DEFS, workspace_id=workspace_id)
        store.save_connection(session, FIXTURE_DSN, master_key=master, workspace_id=workspace_id)
        integrations.install(
            session,
            integrations.load_template("demo_inbox"),
            name="Demo inbox",
            workspace_id=workspace_id,
        )
        integrations.bind(
            session,
            "tell_ops",
            "Demo inbox",
            "post_message",
            {"channel": channel, "text": "order {{ id }}"},
            workspace_id=workspace_id,
        )
        # From the start of history, so the poll has rows to fire on.
        state = session.exec(
            select(TriggerState).where(TriggerState.workspace_id == workspace_id)
        ).one()
        state.watermark = state.watermark.replace(year=2000)
        session.add(state)
    session.commit()

    polled = set()
    for _ in range(2):
        outcome = scheduler.tick(
            session, master_key=master, deliver=actions.deliver, workspace_id=None
        )
        assert outcome is not None and outcome.error is None, outcome
        polled.add(outcome.trigger_name)
    assert scheduler.tick(session, master_key=master, workspace_id=None) is None

    for workspace_id, channel in channels.items():
        deliveries = session.exec(
            select(Delivery).where(Delivery.workspace_id == workspace_id)
        ).all()
        assert deliveries, f"nothing delivered in {channel}'s workspace"
        assert {d.rendered["body"]["channel"] for d in deliveries} == {channel}
