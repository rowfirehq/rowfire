"""Follow refunds through the SDK and print what each component does.

    PYTHONPATH=sdk/python/src uv run python sdk/python/examples/walkthrough.py

Each line says which thread it ran on: the app's own thread (what a request
pays for) or the SDK's background thread (what it does not). The tracing
wraps the SDK's functions from the outside; nothing in the SDK prints.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import time

from sqlalchemy import Integer, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import rowfire_sdk as rowfire
from rowfire_sdk import _client, _spool, _transport, _worker
from rowfire_sdk.integrations import sqlalchemy as hook
from rowfire_sdk.testing import FakeIngest

# -- tracing ------------------------------------------------------------------------


def say(component: str, text: str) -> None:
    thread = threading.current_thread().name
    where = "background" if thread == "rowfire-sdk" else "app thread"
    print(f"  [{where:<10}] {component:<12} {text}")


def trace(owner, name, describe, before=False):
    """Print a line when `owner.name` runs: after it returns, or `before` it
    starts (for steps whose inner work is traced too)."""
    original = getattr(owner, name)

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        if before:
            text = describe(None, *args, **kwargs)
            if text:
                say(*text)
            return original(*args, **kwargs)
        result = original(*args, **kwargs)
        text = describe(result, *args, **kwargs)
        if text:
            say(*text)
        return result

    setattr(owner, name, wrapper)


def watched(result, client, op):
    name = op.name if op else "(none)"
    tables = sorted(client.matcher.resolve(name).tables)
    if result:
        return "matcher", f"{name!r} is watched on {tables}: inspect the write"
    return "matcher", f"{name!r}: nothing subscribes, skip the write (one dict lookup)"


trace(_client.Client, "watches_tables", watched)
trace(
    _client.Client,
    "match_change",
    lambda r, c, op, subs, change: (
        "client",
        f"{change.kind} on {change.table!r}: "
        + (f"matched {[e['subscription'] for e in r]}, held on the session" if r else "no match"),
    ),
)
trace(
    _worker.Worker,
    "add",
    lambda r, w, events: ("worker.add", f"{len(events)} event(s) queued in memory, returning"),
)


def encoded(body, events, lost):
    if events:
        size = len(json.dumps({"events": events}, default=str))
        return "encode", f"batch of {len(events)}: {size} bytes of JSON, {len(body)} gzipped"
    return None


trace(_worker, "encode", encoded)
trace(
    _transport.Transport,
    "send_batch",
    lambda o, t, body, timeout=None: (
        "transport",
        f"POST /v1/events -> {o.kind}"
        + (f" ({o.reason}, retry after {o.retry_after}s)" if o.kind != "ok" else ""),
    ),
)
trace(
    _spool.MemorySpool,
    "put",
    lambda r, s, body, count: ("spool", f"kept {count} event(s) for a retry"),
)
trace(
    _spool.MemorySpool, "remove", lambda r, s, handle: ("spool", "retry delivered, batch removed")
)


# Session listeners are registered by reference, so wrap them before instrument().
def pending(session):
    return len(session.info.get(hook._PENDING, []))


trace(hook, "_after_flush", lambda r, *a: ("orm hook", "after_flush: SQL ran"), before=True)
trace(
    hook,
    "_after_commit",
    lambda r, session: ("orm hook", f"after_commit: hand {pending(session)} held event(s) on"),
    before=True,
)
trace(
    hook,
    "_after_rollback",
    lambda r, session: ("orm hook", f"after_rollback: discard {pending(session)} held event(s)"),
    before=True,
)

# The SDK's own warnings repeat what the trace shows.
logging.getLogger("rowfire_sdk").addHandler(logging.NullHandler())
logging.getLogger("rowfire_sdk").propagate = False

# -- the app ------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String)
    customer_email: Mapped[str] = mapped_column(String)


SUBSCRIPTIONS = [
    {
        "id": "goodwill_refund",
        "operation": "POST /admin/charges/{id}/refund",
        "on": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded", "actor.role": "support"},
        "fields": ["row.id", "row.amount", "old.status", "actor.id"],
    },
    {
        "id": "shipment_failed_refund",
        "operation": "job AutoRefundWorker",
        "on": {"table": "charges", "kind": "update", "changed": "status"},
        "where": {"row.status": "refunded"},
        "fields": ["row.id", "row.account_id"],
    },
]


def refund(engine, charge_id: int, rollback: bool = False) -> None:
    with Session(engine) as session:
        session.get(Charge, charge_id).status = "refunded"
        if rollback:
            session.flush()
            session.rollback()
        else:
            session.commit()


def step(title: str) -> None:
    print(f"\n{title}")


def main() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for i in (1, 2, 3):
            s.add(Charge(id=i, account_id=10 + i, amount=25, status="paid", customer_email="x@y.z"))
        s.commit()

    ingest = FakeIngest().start()

    step("0. Startup: init() compiles the subscriptions; the ORM hook starts listening")
    rowfire.init(
        ingest.url,
        "demo-key",
        subscriptions=SUBSCRIPTIONS,
        flush_interval=0.2,
        backoff_base=0.3,
    )
    say("client", f"{len(SUBSCRIPTIONS)} subscriptions compiled, no ceiling set")
    hook.instrument()
    say("orm hook", "listening on Session after_flush / after_commit / after_rollback")

    step("1. A support agent refunds charge 1 (Rowfire healthy)")
    began = time.perf_counter()
    with rowfire.operation("POST /admin/charges/{id}/refund"):
        say("context", "operation set for this request")
        rowfire.set_actor(id="agent-7", role="support")
        refund(engine, 1)
    say("app", f"request finished in {(time.perf_counter() - began) * 1000:.1f} ms")
    rowfire.flush()
    say("ingest", f"Rowfire has {len(ingest.unique_events)} event(s)")

    step("2. The same write from a job, while Rowfire answers 503 once")
    ingest.fail_next(503, retry_after=0.3)
    with rowfire.operation("job AutoRefundWorker", actor={"role": "system"}):
        refund(engine, 2)
    rowfire.flush()
    ingest.wait_for(2)
    rowfire.flush()
    say("ingest", f"Rowfire has {len(ingest.unique_events)} event(s)")

    step("3. A refund that rolls back")
    with rowfire.operation("job AutoRefundWorker"):
        refund(engine, 3, rollback=True)
    rowfire.flush()
    say("ingest", f"Rowfire still has {len(ingest.unique_events)} event(s)")

    step("4. A write in an operation nobody subscribed to")
    with rowfire.operation("POST /admin/charges/{id}/notes"):
        with Session(engine) as s:
            s.get(Charge, 3).amount = 30
            s.commit()

    rowfire.flush()

    step("What Rowfire received")
    for batch in ingest.batches:
        for event in batch["events"]:
            print(f"  {event['subscription']:<24} {event['operation']:<34} {event['data']}")
    raw = json.dumps(ingest.batches[0]["events"][0], indent=2)
    print("\n  one event as sent (gzipped JSON on the wire):")
    print("\n".join("    " + line for line in raw.splitlines()))

    step("What the dashboard can offer: the catalog, sent alongside events")
    show_catalog(ingest.catalog)
    rowfire.close(timeout=0.5)
    ingest.stop()


def listed(entry: dict) -> str:
    if entry.get("many_values"):
        return "many values"
    return ", ".join(f"{v['value']} ({v['seen']})" for v in entry.get("values", []))


def show_catalog(catalog: dict) -> None:
    print("  operations")
    for name, op in catalog["operations"].items():
        actor = "  ".join(f"actor.{k}: {listed(v)}" for k, v in op.get("actor", {}).items())
        print(f"    {name:<34} called {op['seen']}  {actor}")
        for write in op["writes"]:
            changed = ", ".join(write["changed"]) or "-"
            print(f"      {write['kind']} {write['table']} x{write['seen']}, changing {changed}")
    print("  tables")
    for name, table in catalog["tables"].items():
        print(f"    {name}")
        for column, info in table["columns"].items():
            print(f"      {column:<15} {info['type']:<9} {listed(info)}")
        for column, moves in table["transitions"].items():
            if moves.get("many_values"):
                text = "many values"
            else:
                text = ", ".join(f"{m['from']} -> {m['to']} ({m['seen']})" for m in moves["values"])
            print(f"      {column} changed: {text}")


if __name__ == "__main__":
    main()
