"""The catalog: what the dashboard learns, so people can pick instead of type."""

from __future__ import annotations

import enum

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Enum, ForeignKey, Integer, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import rowfire_sdk as rowfire
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
from rowfire_sdk.integrations.asgi import RowfireMiddleware

REFUND = "POST /admin/charges/{id}/refund"


class Plan(enum.Enum):
    free = "free"
    team = "team"
    enterprise = "enterprise"


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan: Mapped[Plan] = mapped_column(Enum(Plan), default=Plan.free)


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String, default="paid")
    customer_email: Mapped[str] = mapped_column(String, default="a@example.com")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Account(id=1))
        session.add_all(Charge(id=i, account_id=1, amount=10 * i) for i in (1, 2, 3))
        session.commit()
    rowfire_sqlalchemy.instrument()
    yield engine
    rowfire_sqlalchemy.uninstrument()


def refund(engine, charge_id):
    with Session(engine) as session:
        session.get(Charge, charge_id).status = "refunded"
        session.commit()


def catalog(ingest):
    assert rowfire.flush()
    return ingest.catalog


def test_an_unwatched_write_is_described_by_the_operation_that_made_it(db, start, ingest):
    start([])  # nobody subscribes to anything yet: that is the point
    with rowfire.operation(REFUND, actor={"role": "support"}, params={"id": "1"}):
        refund(db, 1)
    with rowfire.operation(REFUND, actor={"role": "support"}, params={"id": "2"}):
        refund(db, 2)

    seen = catalog(ingest)
    op = seen["operations"][REFUND]
    assert op["seen"] == 2
    assert op["actor"] == {"role": {"values": [{"value": "support", "seen": 2}]}}
    assert op["writes"] == [
        {"table": "charges", "kind": "update", "seen": 2, "sampled": 2, "changed": {"status": 2}}
    ]
    assert ingest.events == []  # the catalog is not an event stream


def test_a_table_is_described_from_its_model(db, start, ingest):
    start([])
    with rowfire.operation(REFUND):
        refund(db, 1)
    columns = catalog(ingest)["tables"]["charges"]["columns"]
    assert columns["id"]["primary_key"] is True
    assert columns["amount"]["type"] == "numeric"
    assert columns["account_id"]["references"] == ["accounts.id"]


def test_observed_values_and_transitions_are_listed(db, start, ingest):
    start([])
    with rowfire.operation(REFUND):
        refund(db, 1)
        refund(db, 2)
    table = catalog(ingest)["tables"]["charges"]
    assert table["columns"]["status"]["values"] == [{"value": "refunded", "seen": 2}]
    assert table["transitions"]["status"] == {
        "values": [{"from": "paid", "to": "refunded", "seen": 2}]
    }


def test_an_enum_lists_its_declared_values_before_any_are_seen(db, start, ingest):
    start([])
    with rowfire.operation("POST /accounts/{id}/upgrade"):
        with Session(db) as session:
            session.get(Account, 1).plan = Plan.team
            session.commit()
    plan = catalog(ingest)["tables"]["accounts"]["columns"]["plan"]
    assert plan["declared_values"] == ["free", "team", "enterprise"]
    assert plan["values"] == [{"value": "team", "seen": 1}]


def test_a_column_with_many_values_is_marked_and_its_values_dropped(db, start, ingest):
    start([], catalog_max_values=5)
    with rowfire.operation("POST /checkout"):
        with Session(db) as session:
            for i in range(10):
                session.add(Charge(account_id=1, amount=i, customer_email=f"c{i}@example.com"))
            session.commit()
    columns = catalog(ingest)["tables"]["charges"]["columns"]
    assert columns["customer_email"] == {**columns["customer_email"], "many_values": True}
    assert "values" not in columns["customer_email"]
    assert columns["status"]["values"] == [{"value": "paid", "seen": 10}]


def test_every_write_is_counted_but_only_a_sample_is_read(db, start, ingest):
    start([], catalog_samples_per_minute=3)
    with rowfire.operation("job Backfill"):
        with Session(db) as session:
            for i in range(10):
                session.add(Charge(account_id=1, amount=i))
            session.commit()
    [write] = catalog(ingest)["operations"]["job Backfill"]["writes"]
    assert (write["seen"], write["sampled"]) == (10, 3)


def test_failed_operations_are_counted(start, ingest):
    start([])
    with pytest.raises(RuntimeError):
        with rowfire.operation("job Flaky"):
            raise RuntimeError("boom")
    op = catalog(ingest)["operations"]["job Flaky"]
    assert (op["seen"], op["errors"]) == (1, 1)


def test_routes_are_listed_before_anyone_calls_them(start, ingest):
    app = FastAPI()
    app.add_middleware(RowfireMiddleware)

    @app.post("/accounts/{account_id}/upgrade")
    def upgrade(account_id: str):
        return {}

    @app.delete("/accounts/{account_id}")
    def close_account(account_id: str):
        return {}

    start([])
    with TestClient(app) as http:
        http.post("/accounts/42/upgrade")
    ops = catalog(ingest)["operations"]
    assert ops["DELETE /accounts/{account_id}"]["declared"] is True
    assert ops["DELETE /accounts/{account_id}"]["seen"] == 0
    upgrade_op = ops["POST /accounts/{account_id}/upgrade"]
    assert (upgrade_op["seen"], upgrade_op["statuses"]) == (1, {"200": 1})
    assert upgrade_op["params"] == {"account_id": {"values": [{"value": "42", "seen": 1}]}}


def test_a_denied_column_is_named_but_never_valued(db, start, ingest):
    start([], ceiling=rowfire.Ceiling(deny_fields=["*email*"]))
    with rowfire.operation("POST /checkout"):
        with Session(db) as session:
            session.add(Charge(account_id=1, amount=5))
            session.commit()
    email = catalog(ingest)["tables"]["charges"]["columns"]["customer_email"]
    assert email["denied"] is True
    assert "values" not in email


def test_a_failed_catalog_send_is_carried_by_the_next(start, ingest):
    ingest.fail_next(503, path="/v1/catalog")
    start([])
    with rowfire.operation("job One"):
        pass
    assert rowfire.flush()
    assert ingest.catalog == {}
    with rowfire.operation("job Two"):
        pass
    ops = catalog(ingest)["operations"]
    assert set(ops) == {"job One", "job Two"}  # cumulative: nothing was lost


def test_the_catalog_can_be_turned_off(db, start, ingest):
    start([], catalog=False)
    with rowfire.operation(REFUND):
        refund(db, 1)
    assert rowfire.flush()
    assert ingest.catalogs == {}


def test_a_rolled_back_write_is_not_counted(db, start, ingest):
    start([])
    with rowfire.operation(REFUND):
        refund(db, 1)
        with Session(db) as session:
            session.get(Charge, 2).status = "refunded"
            session.flush()
            session.rollback()
    [write] = catalog(ingest)["operations"][REFUND]["writes"]
    assert write["seen"] == 1
