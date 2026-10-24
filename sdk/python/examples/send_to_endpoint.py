"""Run a small app with the SDK installed and send its traffic to a real URL.

    PYTHONPATH=sdk/python/src uv run python sdk/python/examples/send_to_endpoint.py URL

Point it at a request inspector (Pipedream, webhook.site, RequestBin) to see
exactly what the SDK sends: event batches to `URL/v1/events` and the catalog to
`URL/v1/catalog`. Bodies are plain JSON so the inspector can show them; pass
`--gzip` to send them compressed, as the SDK does by default.

The app is a FastAPI service over an in-memory SQLite database with made-up
data: support agents refund charges, customers check out, a job refunds failed
shipments, and one route is never called.
"""

from __future__ import annotations

import argparse
import json
import sys

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Integer, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

import rowfire_sdk as rowfire
from rowfire_sdk import _transport
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
from rowfire_sdk.integrations.asgi import RowfireMiddleware

SUBSCRIPTIONS = [
    {
        "id": "goodwill_refund",
        "operation": "POST /admin/charges/{charge_id}/refund",
        "write": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded", "actor.role": "support"},
        "fields": ["row.id", "row.account_id", "row.amount", "old.status", "actor.id"],
    },
    {
        "id": "shipment_failed_refund",
        "operation": "job AutoRefundWorker",
        "write": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded"},
        "fields": ["row.id", "row.account_id", "row.amount"],
    },
    {
        "id": "large_checkout",
        "operation": "POST /checkout",
        "write": {"table": "charges", "kind": "insert"},
        "where": {"row.currency": ["usd", "eur"]},
        "fields": ["row.id", "row.account_id", "row.amount", "row.currency", "row.plan"],
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
    plan: Mapped[str] = mapped_column(String, default="team")
    status: Mapped[str] = mapped_column(String, default="paid")


def build_app(engine) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RowfireMiddleware)

    @app.post("/admin/charges/{charge_id}/refund")
    def refund(charge_id: int, agent: str = "agent-7"):
        rowfire.set_actor(id=agent, role="support")
        with Session(engine) as session:
            charge = session.get(Charge, charge_id)
            if charge is None:
                raise HTTPException(404)
            charge.status = "refunded"
            session.commit()
        return {"ok": True}

    @app.post("/checkout")
    def checkout(account_id: int, amount: float, currency: str = "usd", plan: str = "team"):
        rowfire.set_actor(id=f"user-{account_id}", role="customer")
        with Session(engine) as session:
            charge = Charge(account_id=account_id, amount=amount, currency=currency, plan=plan)
            session.add(charge)
            session.commit()
            return {"charge_id": charge.id}

    @app.get("/charges/{charge_id}")
    def show(charge_id: int):
        with Session(engine) as session:
            charge = session.get(Charge, charge_id)
            return {"status": charge.status if charge else None}

    @app.delete("/accounts/{account_id}")
    def close_account(account_id: int):  # never called: still in the catalog
        return {}

    return app


def auto_refund(engine, charge_id: int) -> None:
    with rowfire.operation("job AutoRefundWorker", actor={"role": "system"}):
        with Session(engine) as session:
            session.get(Charge, charge_id).status = "refunded"
            session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="where to send, e.g. a Pipedream request URL")
    parser.add_argument("--gzip", action="store_true", help="compress bodies (the default)")
    parser.add_argument("--show", action="store_true", help="also print each body sent")
    args = parser.parse_args()

    # Print every request the SDK makes, as it makes it.
    original = _transport.Transport.post

    def post(self, path, body, timeout=None):
        outcome = original(self, path, body, timeout)
        print(f"  sent POST {path}  {len(body):>6} bytes of JSON  -> {outcome.kind}")
        if args.show:
            print(json.dumps(json.loads(body), indent=2))
        return outcome

    _transport.Transport.post = post

    # One shared connection: FastAPI runs sync endpoints on worker threads, and
    # each new connection to "sqlite://" would be a fresh, empty database.
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    rowfire.init(
        args.url.rstrip("/"),
        "example-key",
        subscriptions=SUBSCRIPTIONS,
        compress=args.gzip,
        flush_interval=0.5,
    )
    rowfire_sqlalchemy.instrument()

    print(f"Running the app against {args.url}")
    with TestClient(build_app(engine)) as http:
        for account, amount, currency, plan in (
            (11, 49.0, "usd", "team"),
            (12, 990.0, "eur", "enterprise"),
            (13, 15.0, "gbp", "starter"),  # gbp: no event, still in the catalog
        ):
            http.post("/checkout", params=locals_of(account, amount, currency, plan))
        http.post("/admin/charges/1/refund", params={"agent": "agent-7"})
        http.post("/admin/charges/99/refund")  # 404: counted, never an event
        http.get("/charges/2")
    auto_refund(engine, 2)

    rowfire.flush(timeout=10)
    stats = rowfire.stats()
    rowfire.close(timeout=5)
    print(f"Done: {stats['sent']} events sent, lost: {stats['lost'] or 'none'}")
    return 0


def locals_of(account: int, amount: float, currency: str, plan: str) -> dict:
    return {"account_id": account, "amount": amount, "currency": currency, "plan": plan}


if __name__ == "__main__":
    sys.exit(main())
