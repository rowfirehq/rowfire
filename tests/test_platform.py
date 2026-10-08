"""Control-plane persistence: envelope encryption and the fire ledger.

Requires the control-plane Postgres (`docker compose up -d controlplane`) and
a migrated schema (`alembic upgrade head`). Skipped, not failed, without it.
"""

from __future__ import annotations

import base64
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlmodel import Session

from rowfire.definitions import DedupPolicy, Rule, Trigger
from rowfire.platform import crypto, ledger
from rowfire.platform.models import DEFAULT_WORKSPACE_ID

PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # a Monday


def _platform_available() -> bool:
    try:
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM fire LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ crypto
# These need no database.


def _key() -> bytes:
    """A fresh 32-byte master key, as load_master_key would return."""
    return base64.urlsafe_b64decode(crypto.generate_master_key())


def test_encrypt_decrypt_roundtrip() -> None:
    key = _key()
    secret = "postgresql://readonly:hunter2@db.internal:5432/prod"
    envelope = crypto.encrypt(secret, master_key=key)
    assert crypto.decrypt(envelope, master_key=key) == secret


def test_ciphertext_contains_no_plaintext() -> None:
    key = _key()
    secret = "postgresql://readonly:hunter2@db.internal:5432/prod"
    envelope = crypto.encrypt(secret, master_key=key)
    blob = envelope.ciphertext + envelope.wrapped_data_key
    assert b"hunter2" not in blob
    assert b"db.internal" not in blob


def test_each_row_gets_a_distinct_data_key() -> None:
    # Same secret encrypted twice must not produce the same ciphertext, or
    # equal credentials are detectable by comparing columns.
    key = _key()
    first = crypto.encrypt("same-secret", master_key=key)
    second = crypto.encrypt("same-secret", master_key=key)
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_data_key != second.wrapped_data_key


def test_wrong_master_key_fails_loudly() -> None:
    envelope = crypto.encrypt("secret", master_key=_key())
    other = _key()
    with pytest.raises(crypto.CryptoError, match="unwrap"):
        crypto.decrypt(envelope, master_key=other)


def test_tampered_ciphertext_is_rejected() -> None:
    key = _key()
    envelope = crypto.encrypt("secret", master_key=key)
    flipped = bytearray(envelope.ciphertext)
    flipped[0] ^= 0x01
    tampered = crypto.Envelope(
        ciphertext=bytes(flipped),
        nonce=envelope.nonce,
        wrapped_data_key=envelope.wrapped_data_key,
        wrap_nonce=envelope.wrap_nonce,
        key_id=envelope.key_id,
    )
    # AEAD, so this fails rather than yielding garbage that reaches psycopg.
    with pytest.raises(crypto.CryptoError, match="altered"):
        crypto.decrypt(tampered, master_key=key)


def test_rewrap_moves_keys_without_touching_ciphertext() -> None:
    # The whole reason for envelope encryption: adopting a KMS later rewraps
    # data keys and never re-encrypts a stored credential.
    old, new = _key(), _key()
    envelope = crypto.encrypt("secret", master_key=old)
    moved = crypto.rewrap(envelope, old, new, new_key_id="kms/key-1")

    assert moved.ciphertext == envelope.ciphertext  # untouched
    assert moved.wrapped_data_key != envelope.wrapped_data_key
    assert crypto.decrypt(moved, master_key=new) == "secret"
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(moved, master_key=old)


def test_master_key_must_be_32_bytes(monkeypatch) -> None:
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, "c2hvcnQ=")  # "short"
    with pytest.raises(crypto.CryptoError, match="32 bytes"):
        crypto.load_master_key()


def test_missing_master_key_explains_itself(monkeypatch) -> None:
    monkeypatch.delenv(crypto.MASTER_KEY_ENV, raising=False)
    with pytest.raises(crypto.CryptoError, match="not set"):
        crypto.load_master_key()


# ------------------------------------------------------------- dedup keys


def test_dedup_key_is_collision_resistant() -> None:
    # A delimiter join would make these two identical, silently giving two
    # unrelated entities one dedup slot -- one of them would never fire.
    a = ledger.serialise_dedup_key({"x": "a|b", "y": "c"}, ["x", "y"])
    b = ledger.serialise_dedup_key({"x": "a", "y": "b|c"}, ["x", "y"])
    assert a != b


def test_dedup_key_distinguishes_decimal_precision() -> None:
    a = ledger.serialise_dedup_key({"v": Decimal("1.10")}, ["v"])
    b = ledger.serialise_dedup_key({"v": Decimal("1.1")}, ["v"])
    assert a != b


def test_dedup_key_is_stable_across_calls() -> None:
    row = {"customer_id": 7, "city": "Cairo"}
    keys = {ledger.serialise_dedup_key(row, ["customer_id", "city"]) for _ in range(5)}
    assert len(keys) == 1


# ---------------------------------------------------------------- ledger

pytestmark_db = pytest.mark.skipif(
    not _platform_available(),
    reason="control-plane database unavailable; run `docker compose up -d "
    "controlplane` then `alembic upgrade head`",
)


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
        session.commit()


def order(order_id: int, customer_id: int, day_offset: int = 0) -> dict:
    return {
        "id": order_id,
        "customer_id": customer_id,
        "completed_at": BASE + timedelta(days=day_offset),
    }


# The grain lives on the trigger, the cadence on the rule. These two fixtures
# exist as separate module constants because almost every test below needs to
# vary one without touching the other -- which is the whole reason they are
# separate objects in the first place.
BY_ORDER = Trigger(sql="SELECT id, customer_id, completed_at FROM orders", key=["id"])
BY_CUSTOMER = Trigger(
    sql="SELECT id, customer_id, completed_at FROM orders",
    event_time="completed_at",
    key=["customer_id"],
)
BY_ORDER_CLOCKED = Trigger(
    sql="SELECT id, customer_id, completed_at FROM orders",
    event_time="completed_at",
    key=["id"],
)

ONCE_EVER = Rule(trigger="t", policy=DedupPolicy.once_ever)
WEEKLY = Rule(trigger="t", policy=DedupPolicy.once_per_period, period="week")


@pytestmark_db
def test_once_ever_fires_exactly_once(session) -> None:
    first = ledger.claim(
        session,
        rule_name="tell_ops",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=order(1, 10),
    )
    session.commit()
    second = ledger.claim(
        session,
        rule_name="tell_ops",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=order(1, 10),
    )
    session.commit()

    assert first.fired and first.fire_id is not None
    assert not second.fired
    assert second.reason == "already fired"


@pytestmark_db
def test_distinct_rows_each_fire(session) -> None:
    claims = [
        ledger.claim(
            session,
            rule_name="tell_ops",
            trigger=BY_ORDER_CLOCKED,
            rule=ONCE_EVER,
            row=order(i, 10),
        )
        for i in range(5)
    ]
    session.commit()
    assert sum(1 for c in claims if c.fired) == 5


@pytestmark_db
def test_two_rules_on_one_trigger_do_not_suppress_each_other(session) -> None:
    """The reason the ledger keys on the rule and not the trigger.

    If it keyed on the trigger, the first rule to fire for an order would mark
    it fired, and the second rule would look it up, see "already", and silently
    never send -- one automation quietly disabling another. This is the test
    that would have caught that, so it asserts the fire rows directly.
    """
    row = order(1, 10)
    first = ledger.claim(
        session, rule_name="tell_ops", trigger=BY_ORDER_CLOCKED, rule=ONCE_EVER, row=row
    )
    second = ledger.claim(
        session,
        rule_name="thank_the_customer",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=row,
    )
    session.commit()

    assert first.fired and second.fired
    names = {name for (name,) in session.execute(text("SELECT rule_name FROM fire")).all()}
    assert names == {"tell_ops", "thank_the_customer"}


@pytestmark_db
def test_the_same_rule_at_two_grains_is_decided_by_the_trigger(session) -> None:
    # Same rule object, two triggers. The second collapses because its grain
    # is the customer, not the order.
    rows = [order(i, 10, i) for i in range(4)]
    per_order = sum(
        ledger.claim(
            session, rule_name="a", trigger=BY_ORDER_CLOCKED, rule=ONCE_EVER, row=row
        ).fired
        for row in rows
    )
    session.commit()
    per_customer = sum(
        ledger.claim(session, rule_name="b", trigger=BY_CUSTOMER, rule=ONCE_EVER, row=row).fired
        for row in rows
    )
    session.commit()

    assert per_order == 4
    assert per_customer == 1


@pytestmark_db
def test_once_per_period_fires_once_per_bucket(session) -> None:
    # Same customer, three separate weeks.
    fired = 0
    for day in (0, 7, 14):
        claim = ledger.claim(
            session,
            rule_name="weekly",
            trigger=BY_CUSTOMER,
            rule=WEEKLY,
            row=order(day, 10, day),
        )
        session.commit()
        fired += claim.fired
    assert fired == 3


@pytestmark_db
def test_once_per_period_collapses_within_a_bucket(session) -> None:
    # The fixture's power customer: many orders inside a single week.
    fired = 0
    for i in range(40):
        claim = ledger.claim(
            session,
            rule_name="weekly",
            trigger=BY_CUSTOMER,
            rule=WEEKLY,
            row=order(i, 10, i % 5),  # all within one Mon-Fri
        )
        session.commit()
        fired += claim.fired
    assert fired == 1


@pytestmark_db
def test_two_cadences_on_one_trigger_keep_separate_ledgers(session) -> None:
    # The demo case: weekly and daily rules on the same query. Each keeps its
    # own slots, so the daily one is not starved by the weekly one.
    daily = Rule(trigger="t", policy=DedupPolicy.once_per_period, period="day")
    weekly_fires = daily_fires = 0
    for day in range(7):
        row = order(day, 10, day)
        weekly_fires += ledger.claim(
            session, rule_name="weekly", trigger=BY_CUSTOMER, rule=WEEKLY, row=row
        ).fired
        daily_fires += ledger.claim(
            session, rule_name="daily", trigger=BY_CUSTOMER, rule=daily, row=row
        ).fired
        session.commit()

    assert weekly_fires == 1
    assert daily_fires == 7


@pytestmark_db
def test_once_per_period_refuses_rows_without_event_time(session) -> None:
    claim = ledger.claim(
        session,
        rule_name="weekly",
        trigger=BY_CUSTOMER,
        rule=WEEKLY,
        row={"id": 1, "customer_id": 10, "completed_at": None},
    )
    session.commit()
    # Not fired, and explicitly explained -- never folded into one shared
    # bucket where the first null row would fire and the rest never would.
    assert not claim.fired
    assert "event_time" in (claim.reason or "")


@pytestmark_db
def test_once_per_n_fires_on_every_nth(session) -> None:
    rule = Rule(trigger="t", policy=DedupPolicy.once_per_n, n=3)
    fired = []
    for i in range(10):
        claim = ledger.claim(
            session,
            rule_name="every_third",
            trigger=BY_CUSTOMER,
            rule=rule,
            row=order(i, 10, i),
        )
        session.commit()
        if claim.fired:
            fired.append(i)
    assert fired == [0, 3, 6, 9]


@pytestmark_db
def test_once_per_n_without_a_clock_is_refused(session) -> None:
    # Regression. The occurrence identity used to come from the key's first
    # column, so a trigger keyed on the customer counted one occurrence ever
    # and "every 3rd order" quietly meant "the first order, once". A trigger
    # with no clock has nothing to tell occurrences apart, so it is refused
    # out loud instead.
    rule = Rule(trigger="t", policy=DedupPolicy.once_per_n, n=3)
    claim = ledger.claim(
        session, rule_name="no_clock", trigger=BY_ORDER, rule=rule, row=order(1, 10)
    )
    session.commit()
    assert not claim.fired
    assert "event_time" in (claim.reason or "")


@pytestmark_db
def test_once_per_n_counts_rows_not_keys(session) -> None:
    # Ten orders from one customer are ten occurrences, not one -- even though
    # the dedup key is the customer and never changes.
    rule = Rule(trigger="t", policy=DedupPolicy.once_per_n, n=1)
    fired = 0
    for i in range(10):
        fired += ledger.claim(
            session,
            rule_name="count_them",
            trigger=BY_CUSTOMER,
            rule=rule,
            row=order(i, 10, i),
        ).fired
        session.commit()
    assert fired == 10


@pytestmark_db
def test_once_per_n_is_idempotent_under_replay(session) -> None:
    # A run that dies after counting but before sending must not shift the
    # schedule when it is retried.
    rule = Rule(trigger="t", policy=DedupPolicy.once_per_n, n=3)
    rows = [order(i, 10, i) for i in range(6)]

    for row in rows:
        ledger.claim(session, rule_name="replayed", trigger=BY_CUSTOMER, rule=rule, row=row)
        session.commit()

    replayed = []
    for row in rows:  # the whole run again
        claim = ledger.claim(session, rule_name="replayed", trigger=BY_CUSTOMER, rule=rule, row=row)
        session.commit()
        replayed.append(claim.fired)

    assert not any(replayed), "replaying a run must not fire anything again"

    counted = session.execute(
        text("SELECT seen_count FROM dedup_counter WHERE rule_name = 'replayed'")
    ).scalar_one()
    assert counted == 6, "replay must not advance the occurrence counter"


@pytestmark_db
def test_concurrent_claims_produce_exactly_one_fire(engine) -> None:
    """The reliability guarantee, tested the only way that means anything.

    Sixteen workers race to claim the same row. A SELECT-then-INSERT would
    let several through; the unique constraint plus ON CONFLICT DO NOTHING
    lets exactly one.
    """
    row = order(99, 10)

    def attempt(_: int) -> bool:
        with Session(engine) as session:
            claim = ledger.claim(
                session,
                rule_name="racy",
                trigger=BY_ORDER_CLOCKED,
                rule=ONCE_EVER,
                row=row,
            )
            session.commit()
            return claim.fired

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(16)))

    assert sum(results) == 1, f"expected exactly one winner, got {sum(results)}"

    with Session(engine) as session:
        rows = session.execute(
            text("SELECT count(*) FROM fire WHERE rule_name = 'racy'")
        ).scalar_one()
    assert rows == 1


@pytestmark_db
def test_concurrent_distinct_rows_all_fire(engine) -> None:
    # The flip side: contention must not suppress genuinely distinct fires.
    def attempt(i: int) -> bool:
        with Session(engine) as session:
            claim = ledger.claim(
                session,
                rule_name="parallel",
                trigger=BY_ORDER_CLOCKED,
                rule=ONCE_EVER,
                row=order(i, 10),
            )
            session.commit()
            return claim.fired

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(32)))

    assert sum(results) == 32


@pytestmark_db
def test_has_fired_reflects_the_ledger(session) -> None:
    claim = ledger.claim(
        session,
        rule_name="tell_ops",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=order(5, 10),
    )
    session.commit()
    assert ledger.has_fired(
        session,
        rule_name="tell_ops",
        dedup_key=claim.dedup_key,
        dedup_bucket=claim.dedup_bucket,
    )


@pytestmark_db
def test_payload_is_stored_for_audit(session) -> None:
    claim = ledger.claim(
        session,
        rule_name="tell_ops",
        trigger=BY_ORDER,  # no event_time: a timeless trigger still audits
        rule=ONCE_EVER,
        row={"id": 7, "customer_id": 3, "total_amount": Decimal("12.50")},
    )
    session.commit()
    payload = session.execute(
        text("SELECT payload FROM fire WHERE id = :id"), {"id": claim.fire_id}
    ).scalar_one()
    # Decimal kept as a string so the audit record does not silently become
    # a float somewhere between here and a customer's invoice.
    assert payload["total_amount"] == "12.50"


@pytestmark_db
def test_workspace_is_part_of_the_unique_key(session) -> None:
    # Single-tenant today, but the constraint is already the right shape, so
    # going multi-tenant is not a unique-index rebuild on the biggest table.
    other = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
    row = order(1, 10)
    first = ledger.claim(
        session,
        rule_name="scoped",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=row,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    session.commit()
    second = ledger.claim(
        session,
        rule_name="scoped",
        trigger=BY_ORDER_CLOCKED,
        rule=ONCE_EVER,
        row=row,
        workspace_id=other,
    )
    session.commit()
    assert first.fired and second.fired
