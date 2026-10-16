"""Writes tagged with the operation that caused them, released on commit only."""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import rowfire_sdk as rowfire
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy

SUPPORT_REFUND = "POST /admin/charges/{id}/refund"
AUTO_REFUND = "job AutoRefundWorker"

# The same write, `charges.status -> refunded`, is two different business
# events depending on which operation made it.
SUBSCRIPTIONS = [
    {
        "id": "goodwill_refund",
        "operation": SUPPORT_REFUND,
        "on": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded", "actor.role": "support"},
        "fields": ["row.id", "row.amount", "old.status", "actor.id", "params.id"],
    },
    {
        "id": "shipment_failed_refund",
        "operation": AUTO_REFUND,
        "on": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded"},
        "fields": ["row.id", "row.account_id"],
    },
    {
        "id": "large_charge",
        "operation": "POST /checkout",
        "on": {"table": "charges", "kind": "insert"},
        "where": {"row.currency": ["usd", "eur"]},
        "fields": ["row.id", "row.amount"],
    },
]


class Base(DeclarativeBase):
    pass


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String, default="usd")
    status: Mapped[str] = mapped_column(String, default="paid")
    customer_email: Mapped[str] = mapped_column(String, default="a@example.com")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Charge(id=1, account_id=10, amount=25))
        session.commit()
    rowfire_sqlalchemy.instrument()
    yield engine
    rowfire_sqlalchemy.uninstrument()


def refund(engine, charge_id=1):
    with Session(engine) as session:
        session.get(Charge, charge_id).status = "refunded"
        session.commit()


def test_a_support_refund_is_a_goodwill_refund(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(SUPPORT_REFUND, params={"id": "1"}):
        rowfire.set_actor(id="agent-7", role="support")
        refund(db)
    assert rowfire.flush()
    [event] = ingest.events
    assert event["subscription"] == "goodwill_refund"
    assert event["operation"] == SUPPORT_REFUND
    assert event["data"] == {
        "row.id": 1,
        "row.amount": "25.00",
        "old.status": "paid",
        "actor.id": "agent-7",
        "params.id": "1",
    }


def test_the_same_write_from_a_job_is_a_different_event(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND, actor={"role": "system"}):
        refund(db)
    assert rowfire.flush()
    assert [e["subscription"] for e in ingest.events] == ["shipment_failed_refund"]
    assert ingest.events[0]["data"] == {"row.id": 1, "row.account_id": 10}


def test_a_rolled_back_refund_is_never_announced(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND):
        with Session(db) as session:
            session.get(Charge, 1).status = "refunded"
            session.flush()  # matched here, held on the session
            session.rollback()
    assert rowfire.flush()
    assert ingest.events == []


def test_nothing_leaves_before_commit(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND):
        with Session(db) as session:
            session.get(Charge, 1).status = "refunded"
            session.flush()
            assert rowfire.flush()
            assert ingest.events == []
            session.commit()
    assert rowfire.flush()
    assert len(ingest.events) == 1


def test_writes_outside_a_watched_operation_send_nothing(db, start, ingest):
    start(SUBSCRIPTIONS)
    refund(db)  # no operation at all
    with rowfire.operation("POST /admin/charges/{id}/notes"):
        refund(db)
    assert rowfire.flush()
    assert ingest.events == []


def test_an_update_that_does_not_change_the_watched_column_is_ignored(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND):
        refund(db)
        with Session(db) as session:
            session.get(Charge, 1).amount = 30  # status was already refunded
            session.commit()
    assert rowfire.flush()
    assert len(ingest.events) == 1


def test_inserts_get_their_generated_key(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation("POST /checkout"):
        with Session(db) as session:
            session.add(Charge(account_id=11, amount=900, currency="eur"))
            session.add(Charge(account_id=12, amount=5, currency="gbp"))
            session.commit()
    assert rowfire.flush()
    [event] = ingest.events
    # Values are what the app holds after the flush, not re-read from the database.
    assert event["data"] == {"row.id": 2, "row.amount": 900}


def test_a_denied_column_cannot_be_subscribed_to(db, start, ingest):
    leaky = {**SUBSCRIPTIONS[1], "id": "leaky", "fields": ["row.customer_email"]}
    client = start([leaky], ceiling=rowfire.Ceiling(deny_fields=["*email*"]))
    with rowfire.operation(AUTO_REFUND):
        refund(db)
    assert rowfire.flush()
    assert ingest.events == []
    assert client.stats()["rejected_subscriptions"][0][0] == "leaky"


def test_setting_a_column_to_the_value_it_had_is_not_a_change(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND):
        with Session(db) as session:
            session.get(Charge, 1).status = "paid"  # already paid
            session.commit()
    assert rowfire.flush()
    assert ingest.events == []


def test_a_column_changed_before_it_was_loaded_still_fires(db, start, ingest):
    start(SUBSCRIPTIONS)
    with rowfire.operation(AUTO_REFUND):
        with Session(db) as session:
            charge = session.get(Charge, 1)
            session.expire(charge, ["status"])
            charge.status = "refunded"  # set without ever reading the old value
            session.commit()
    assert rowfire.flush()
    assert [e["subscription"] for e in ingest.events] == ["shipment_failed_refund"]
