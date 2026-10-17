"""What the SDK costs the app.

    PYTHONPATH=sdk/python/src uv run python sdk/python/bench/bench_overhead.py

Four measurements, kept apart because they answer different questions:

1. Request thread: what one operation or one write costs the code that runs it,
   with the background thread idle.
2. Background thread: CPU per event spent encoding and compressing. It runs off
   the request's path, but in CPython it holds the GIL while it works, so this
   is the share of the app's CPU that delivery takes.
3. ORM writes: an update and commit with and without the hook, warmed up and
   interleaved so drift lands on every case alike. On a shared machine the
   spread between runs (tens of µs) is larger than the hook's own cost, so
   read these deltas as "within noise" unless they are large.
4. A slow Rowfire: per-call latency while every batch takes a second to be
   answered, next to an instant one. If the network were on the request's
   path, the slow column would show it.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

from sqlalchemy import Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import rowfire_sdk as rowfire
from rowfire_sdk._worker import encode
from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
from rowfire_sdk.testing import FakeIngest

SUBSCRIPTIONS = [
    {
        "id": "checkout_completed",
        "operation": "POST /checkout",
        "fields": ["params.plan", "actor.id"],
    },
    {
        "id": "charge_refunded",
        "operation": "job AutoRefundWorker",
        "on": {"table": "charges", "kind": "update", "changed": "status"},
        "fields": ["row.id", "old.status", "row.status"],
    },
]


class Base(DeclarativeBase):
    pass


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String)


def bare() -> None:
    pass


def unwatched() -> None:
    with rowfire.operation("GET /health"):
        pass


def watched() -> None:
    with rowfire.operation("POST /checkout", actor={"id": "u1"}, params={"plan": "team"}):
        pass


def micros(fn: Callable[[], None], n: int, between: Callable[[], None] = bare) -> float:
    """Median µs per call over five runs of n calls."""
    samples = []
    for _ in range(5):
        between()
        began = time.perf_counter_ns()
        for _ in range(n):
            fn()
        samples.append((time.perf_counter_ns() - began) / n / 1000)
    return statistics.median(samples)


def request_thread(rows: list) -> None:
    # Unreachable and never woken: the background thread does not run, and the
    # buffer is emptied between runs so its size does not skew the timing.
    n = 20_000
    rows.append(("empty function call (baseline)", micros(bare, n), ""))
    for catalog in (False, True):
        client = rowfire.init(
            "http://127.0.0.1:9",
            "bench",
            subscriptions=SUBSCRIPTIONS,
            batch_size=10**9,
            flush_interval=3600,
            catalog=catalog,
        )

        def empty_buffer(client=client) -> None:
            client.worker._buffer.clear()

        label = "catalog on" if catalog else "catalog off"
        rows.append(
            (f"operation nobody subscribes to, {label}", micros(unwatched, n, empty_buffer), "")
        )
        rows.append((f"operation that matches, {label}", micros(watched, n, empty_buffer), ""))
        rowfire.close(timeout=0)


def background_thread(rows: list) -> None:
    events = [
        {
            "id": "0" * 32,
            "subscription": "checkout_completed",
            "config_version": "v1",
            "operation": "POST /checkout",
            "occurred_at": time.time(),
            "data": {"params.plan": "team", "actor.id": "u1"},
        }
    ] * 200
    per_batch = micros(lambda: encode(events, {}), 200)
    rows.append(("encode + gzip, per event (background thread)", per_batch / 200, ""))


def orm_writes(rows: list) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Charge(id=1, status="paid"))
        s.commit()
    flip = iter(["refunded", "paid"] * 10**7)

    def write(op_name: str) -> Callable[[], None]:
        def run() -> None:
            with rowfire.operation(op_name):
                with Session(engine) as s:
                    s.get(Charge, 1).status = next(flip)
                    s.commit()

        return run

    client = rowfire.init(
        "http://127.0.0.1:9", "bench", subscriptions=SUBSCRIPTIONS, batch_size=10**9
    )
    cases = {
        "not instrumented": (False, write("job AutoRefundWorker")),
        "instrumented, unwatched op": (True, write("job SomethingElse")),
        "instrumented, matched": (True, write("job AutoRefundWorker")),
    }
    samples: dict[str, list[float]] = {name: [] for name in cases}
    n = 500
    for round_ in range(11):
        for name, (hooked, fn) in cases.items():
            if hooked:
                rowfire_sqlalchemy.instrument()
            client.worker._buffer.clear()
            began = time.perf_counter_ns()
            for _ in range(n):
                fn()
            elapsed = (time.perf_counter_ns() - began) / n / 1000
            rowfire_sqlalchemy.uninstrument()
            if round_:  # the first round warms up
                samples[name].append(elapsed)
    base = statistics.median(samples["not instrumented"])
    for name, values in samples.items():
        median = statistics.median(values)
        delta = "" if name == "not instrumented" else f"{median - base:+.1f} µs"
        rows.append((f"ORM update + commit, {name}", median, delta))
    rowfire.close(timeout=0)


def slow_rowfire() -> list[str]:
    """Per-call latency in a loop paced like a busy app, against a Rowfire that
    answers at once and one that takes a second. The unsubscribed operation in
    the same loop is the control: what the pacing alone costs."""
    lines = []
    for label, delay, fn in (
        ("control, operation nobody subscribes to", 0.0, unwatched),
        ("matched, Rowfire answers at once", 0.0, watched),
        ("matched, Rowfire takes 1s per batch", 1.0, watched),
    ):
        ingest = FakeIngest().start()
        ingest.delay = delay
        rowfire.init(ingest.url, "bench", subscriptions=SUBSCRIPTIONS, batch_size=100)
        latencies = []
        stop = time.monotonic() + 3.0
        while time.monotonic() < stop:
            began = time.perf_counter_ns()
            fn()
            latencies.append((time.perf_counter_ns() - began) / 1000)
            time.sleep(0.0005)  # ~2,000 operations a second
        lost = sum(rowfire.stats()["lost"].values())
        rowfire.close(timeout=0.1)
        ingest.stop()
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p99 = latencies[int(len(latencies) * 0.99)]
        lines.append(
            f"{label:<42} p50 {p50:6.1f} µs  p99 {p99:6.1f} µs  "
            f"max {latencies[-1]:7.0f} µs  ({len(latencies):,} calls, lost {lost})"
        )
    return lines


def main() -> None:
    rows: list[tuple[str, float, str]] = []
    request_thread(rows)
    background_thread(rows)
    orm_writes(rows)
    width = max(len(r[0]) for r in rows)
    print(f"{'case':<{width}}  {'µs':>8}  delta")
    for name, value, note in rows:
        print(f"{name:<{width}}  {value:>8.2f}  {note}")
    print()
    for line in slow_rowfire():
        print(line)


if __name__ == "__main__":
    main()
