"""The background half: batching, sending, retrying, spooling.

The request thread's only call is `add()`, which appends to a list under a lock
and returns. Everything else happens on one daemon thread per process.

Patterns taken from sentry-python (`sentry_sdk/_batcher.py`, `worker.py`):

- the thread is started lazily and owned by a pid, and `os.register_at_fork`
  resets the state in a child, so gunicorn or Celery workers each get their own
  thread rather than inheriting a dead one;
- a thread-local guard drops a re-entrant `add()` made from inside the worker
  (a log handler that reports to us, say) instead of deadlocking on our lock;
- the flush wait is jittered, so a fleet of processes does not flush in step;
- an exception in one cycle is logged and the loop carries on, since a dead
  thread would leave the buffer filling with nothing draining it.

Where it departs from Sentry, deliberately: a failed send is kept and retried
with backoff (Sentry drops it), and a batch waiting to be retried goes to the
spool, which can be on disk.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import random
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Callable

from rowfire_sdk._transport import OK, RETRY, Transport
from rowfire_sdk._version import VERSION

log = logging.getLogger("rowfire_sdk")


class Counters:
    """Delivery counters. Losses ride along with the next batch, as Sentry's
    client reports do, so Rowfire can show a rule owner what never arrived."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # `lost` is what has not been reported yet; `lost_total` never resets.
        self.lost: dict[str, int] = {}
        self.lost_total: dict[str, int] = {}
        self.sent = 0
        self.spooled = 0

    def lose(self, reason: str, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.lost[reason] = self.lost.get(reason, 0) + n
            self.lost_total[reason] = self.lost_total.get(reason, 0) + n

    def take_lost(self) -> dict[str, int]:
        with self._lock:
            lost, self.lost = self.lost, {}
        return lost

    def reset_after_fork(self) -> None:
        # The parent reports its own unreported losses; and its lock may have
        # been held by another thread at the moment of the fork.
        self._lock = threading.Lock()
        self.lost = {}

    def restore_lost(self, lost: dict[str, int]) -> None:
        """Put back counts whose report did not get through."""
        with self._lock:
            for reason, n in lost.items():
                self.lost[reason] = self.lost.get(reason, 0) + n


class Worker:
    def __init__(
        self,
        transport: Transport,
        spool: Any,
        counters: Counters,
        *,
        batch_size: int = 200,
        max_queue: int = 10_000,
        flush_interval: float = 1.0,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        on_tick: Callable[[bool], None] | None = None,
        on_fork: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._spool = spool
        self._counters = counters
        self._batch_size = batch_size
        self._max_queue = max_queue
        self._flush_interval = flush_interval
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._on_tick = on_tick
        self._on_fork = on_fork
        self._reset_state()
        if hasattr(os, "register_at_fork"):
            weak = weakref.WeakMethod(self._after_fork)

            def reset_in_child() -> None:
                method = weak()
                if method is not None:
                    method()

            os.register_at_fork(after_in_child=reset_in_child)

    def _reset_state(self) -> None:
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._wake = threading.Event()
        self._waiters: list[threading.Event] = []
        self._guard = threading.local()
        self._thread: threading.Thread | None = None
        self._pid: int | None = None
        self._running = True
        self._final_deadline: float | None = None
        self._failures = 0
        self._retry_at = 0.0

    def _after_fork(self) -> None:
        # The parent still holds what it buffered and will send it; sending it
        # from the child too would deliver it twice. The connection's socket is
        # shared with the parent, so it must not be used here either.
        self._reset_state()
        self._counters.reset_after_fork()
        self._spool.reset()
        self._transport.abandon()
        if self._on_fork is not None:
            self._on_fork()

    # -- request thread -------------------------------------------------------

    def add(self, events: list[dict[str, Any]]) -> None:
        if getattr(self._guard, "active", False):
            return
        if not self.ensure_thread():
            self._counters.lose("worker_stopped", len(events))
            return
        dropped = 0
        with self._lock:
            room = self._max_queue - len(self._buffer)
            if room < len(events):
                dropped = len(events) - max(room, 0)
                events = events[: max(room, 0)]
            self._buffer.extend(events)
            full = len(self._buffer) >= self._batch_size
        if dropped:
            self._counters.lose("queue_overflow", dropped)
        if full:
            self._wake.set()

    def ensure_thread(self) -> bool:
        if not self._running:
            return False
        pid = os.getpid()
        if self._pid == pid:
            return True
        with self._lock:
            if self._pid == pid:
                return True
            thread = threading.Thread(target=self._loop, name="rowfire-sdk", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                # The interpreter is shutting down and will not start threads.
                self._running = False
                return False
            self._thread = thread
            self._pid = pid
        return True

    def queued(self) -> int:
        return len(self._buffer)

    # -- callers that may wait ------------------------------------------------

    def flush(self, timeout: float = 2.0) -> bool:
        """Send what is buffered now; True if that finished within `timeout`.

        "Finished" means delivered, or kept in the spool for a retry.
        """
        if self._thread is None or self._pid != os.getpid() or not self._running:
            return not self._buffer
        done = threading.Event()
        with self._lock:
            self._waiters.append(done)
        self._wake.set()
        return done.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        """Stop the thread after one last cycle, bounded by `timeout`.

        Whatever cannot be delivered by then goes to the spool, so with a disk
        spool the next process sends it.
        """
        thread = self._thread
        self._final_deadline = time.monotonic() + timeout
        self._running = False
        self._wake.set()
        if thread is not None and thread.is_alive() and self._pid == os.getpid():
            thread.join(timeout + 0.5)
        self._transport.close()

    # -- background thread ----------------------------------------------------

    def _loop(self) -> None:
        self._guard.active = True
        while self._running:
            self._wake.wait(self._flush_interval * (1 + 0.2 * random.random()))
            self._wake.clear()
            with self._lock:
                waiters, self._waiters = self._waiters, []
            self._run_safely(self._tick)
            self._run_safely(self._cycle)
            for waiter in waiters:
                waiter.set()
        self._run_safely(lambda: self._cycle(self._final_deadline))
        self._run_safely(lambda: self._tick(final=True))
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.set()

    def _run_safely(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("rowfire-sdk: background cycle failed")

    def _tick(self, final: bool = False) -> None:
        if self._on_tick is not None:
            self._on_tick(final)

    def _cycle(self, deadline: float | None = None) -> None:
        with self._lock:
            pending, self._buffer = self._buffer, []
        for start in range(0, len(pending), self._batch_size):
            chunk = pending[start : start + self._batch_size]
            lost = self._counters.take_lost()
            body = encode(chunk, lost)
            if self._backing_off() or _past(deadline):
                self._keep(body, len(chunk))
                continue
            self._deliver(body, len(chunk), lost, deadline, spooled=None)
        # A batch with no events still carries loss counts.
        if not pending and self._counters.lost and not self._backing_off():
            lost = self._counters.take_lost()
            self._deliver(encode([], lost), 0, lost, deadline, spooled=None)
        self._drain_spool(deadline)

    def _drain_spool(self, deadline: float | None) -> None:
        while not self._backing_off() and not _past(deadline):
            try:
                batch = self._spool.peek()
            except OSError:
                log.exception("rowfire-sdk: cannot read the spool")
                return
            if batch is None:
                return
            handle, body, count = batch
            if not self._deliver(body, count, {}, deadline, spooled=handle):
                return

    def _deliver(
        self,
        body: bytes,
        count: int,
        lost: dict[str, int],
        deadline: float | None,
        spooled: Any,
    ) -> bool:
        """Send one batch; True if the caller may carry on with the next."""
        timeout = None if deadline is None else max(0.1, deadline - time.monotonic())
        outcome = self._transport.send_batch(body, timeout)
        if outcome.kind == OK:
            self._failures = 0
            self._counters.sent += count
            if spooled is not None:
                self._spool.remove(spooled)
            return True
        if outcome.kind == RETRY:
            self._failures += 1
            delay = outcome.retry_after
            if delay is None:
                delay = min(self._backoff_max, self._backoff_base * 2 ** (self._failures - 1))
                delay *= 0.5 + random.random()
            self._retry_at = time.monotonic() + delay
            log.warning("rowfire-sdk: send failed (%s), retrying in %.1fs", outcome.reason, delay)
            if spooled is None:
                self._keep(body, count)
            return False
        # Rejected: this body will never be accepted, so it must not block the rest.
        log.error("rowfire-sdk: batch rejected (%s), %d events dropped", outcome.reason, count)
        self._counters.lose(f"rejected_{outcome.reason}", count)
        self._counters.restore_lost(lost)
        if spooled is not None:
            self._spool.remove(spooled)
        return True

    def _keep(self, body: bytes, count: int) -> None:
        try:
            evicted = self._spool.put(body, count)
        except OSError:
            log.exception("rowfire-sdk: cannot write to the spool")
            self._counters.lose("spool_error", count)
            return
        self._counters.spooled += count
        self._counters.lose("spool_overflow", evicted)

    def _backing_off(self) -> bool:
        return time.monotonic() < self._retry_at


def encode(events: list[dict[str, Any]], lost: dict[str, int]) -> bytes:
    payload = {
        "sdk": {"name": "rowfire-sdk-python", "version": VERSION},
        "encoded_at": datetime.now(timezone.utc).isoformat(),
        "events": [_formatted(e) for e in events],
        "lost": lost,
    }
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return gzip.compress(raw, compresslevel=6)


def _formatted(event: dict[str, Any]) -> dict[str, Any]:
    """Format the timestamp here rather than on the request thread."""
    occurred_at = event.get("occurred_at")
    if isinstance(occurred_at, float):
        stamp = datetime.fromtimestamp(occurred_at, timezone.utc).isoformat()
        return {**event, "occurred_at": stamp}
    return event


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
