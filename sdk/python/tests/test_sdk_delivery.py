"""The background half: batching, retries, the spool, forks and config."""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime

import pytest

import rowfire_sdk as rowfire
from rowfire_sdk._spool import DiskSpool
from rowfire_sdk._worker import encode
from rowfire_sdk.testing import FakeIngest

CHECKOUT = {"id": "checkout", "operation": "POST /checkout", "fields": ["params.n"]}


def checkout(n: int) -> None:
    with rowfire.operation("POST /checkout", params={"n": n}):
        pass


def unused_url() -> str:
    """An address nothing listens on, so every send is refused."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_events_arrive_in_batches(start, ingest):
    start([CHECKOUT], batch_size=10)
    for n in range(25):
        checkout(n)
    assert rowfire.flush()
    assert [e["data"]["params.n"] for e in ingest.events] == list(range(25))
    assert all(len(b["events"]) <= 10 for b in ingest.batches)
    assert ingest.events[0]["operation"] == "POST /checkout"
    # Recorded as a float on the request thread, formatted in the background.
    assert datetime.fromisoformat(ingest.events[0]["occurred_at"]).tzinfo is not None


def test_a_slow_rowfire_never_slows_the_app(start, ingest):
    ingest.delay = 1.0
    start([CHECKOUT], batch_size=5)
    began = time.perf_counter()
    for n in range(500):
        checkout(n)
    elapsed = time.perf_counter() - began
    # 500 operations while every send takes a second: the request side only queues.
    assert elapsed < 0.25, elapsed


def test_a_failed_send_is_retried_until_it_lands(start, ingest):
    ingest.fail_next(503, times=2, retry_after=0.05)
    start([CHECKOUT])
    checkout(1)
    assert ingest.wait_for(1)
    assert ingest.requests >= 3
    assert len(ingest.unique_events) == 1


def test_a_rejected_batch_is_dropped_and_reported(start, ingest):
    ingest.fail_next(400)
    start([CHECKOUT])
    checkout(1)
    assert rowfire.flush()
    checkout(2)
    assert ingest.wait_for(1)
    assert [e["data"]["params.n"] for e in ingest.events] == [2]
    assert ingest.lost == {"rejected_status_400": 1}
    assert rowfire.stats()["lost"] == {"rejected_status_400": 1}


def test_past_the_hard_limit_the_queue_drops_and_counts_instead_of_blocking(start, ingest):
    ingest.delay = 0.5
    client = start([CHECKOUT], max_queue=5, max_queue_hard=10, batch_size=1000, flush_interval=60)
    for n in range(15):
        checkout(n)
    assert client.stats()["lost"] == {"queue_overflow": 5}


def test_a_burst_past_the_soft_limit_is_spooled_not_dropped(start, ingest):
    # One commit handing over far more events than max_queue at once, like a
    # job that updates thousands of bookings in a single transaction.
    client = start([CHECKOUT], max_queue=100, max_queue_hard=5_000, batch_size=50)
    events = [
        client._event(
            client.matcher.resolve("POST /checkout").on_complete[0],
            "POST /checkout",
            {"params.n": n},
        )
        for n in range(1_500)
    ]
    client.emit(events)
    assert rowfire.flush(timeout=10)
    assert sorted(e["data"]["params.n"] for e in ingest.unique_events) == list(range(1_500))
    stats = client.stats()
    assert stats["lost"] == {}
    assert stats["spooled"] == 1_500  # went through the spool, compact, not as objects


def test_events_waiting_in_the_spool_go_out_before_newer_ones(start, ingest):
    client = start([CHECKOUT], flush_interval=3600)  # one cycle, run by hand below
    sub = client.matcher.resolve("POST /checkout").on_complete[0]
    older = client._event(sub, "POST /checkout", {"params.n": 1})
    newer = client._event(sub, "POST /checkout", {"params.n": 2})
    client._spool.put(encode([older], {}), 1)  # left from a failed send
    client.worker._buffer.append(newer)
    client.worker._cycle()
    assert [e["data"]["params.n"] for e in ingest.events] == [1, 2]


def test_while_rowfire_is_down_batches_wait_in_the_spool(tmp_path, ingest, start):
    down = unused_url()
    client = start([CHECKOUT], url=down, spool_dir=str(tmp_path))
    for n in range(3):
        checkout(n)
    client.flush()
    assert client.stats()["waiting_in_spool"] == 3
    rowfire.close(timeout=0.5)

    # The next process, with Rowfire back, sends what the last one left.
    start([CHECKOUT], spool_dir=str(tmp_path))
    assert ingest.wait_for(3)
    assert sorted(e["data"]["params.n"] for e in ingest.unique_events) == [0, 1, 2]


def test_a_dead_processs_spool_is_adopted(tmp_path, ingest, start):
    dead = _dead_pid()
    body = encode([{"id": "e1", "subscription": "checkout", "data": {}}], {})
    with open(tmp_path / f"{1:020d}-{0:08d}-{dead}-1.batch", "wb") as f:
        f.write(body)
    start([CHECKOUT], spool_dir=str(tmp_path))
    checkout(1)  # starts the worker, which drains the spool
    assert ingest.wait_for(2)
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".batch")]


def test_a_live_processs_spool_is_left_alone(tmp_path):
    other = os.getppid()
    with open(tmp_path / f"{1:020d}-{0:08d}-{other}-1.batch", "wb") as f:
        f.write(b"x")
    assert DiskSpool(str(tmp_path)).peek() is None


def test_the_spool_cap_evicts_the_oldest_and_counts_it(tmp_path):
    spool = DiskSpool(str(tmp_path), max_bytes=250)
    evicted = [spool.put(b"x" * 100, n) for n in (1, 2, 3)]
    assert evicted == [0, 0, 1]
    assert spool.pending() == 5


def test_close_sends_what_is_left(start, ingest):
    start([CHECKOUT], flush_interval=60)
    checkout(1)
    rowfire.close(timeout=2)
    assert len(ingest.events) == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
# Forking a process that has threads is exactly what gunicorn does to us.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_worker_sends_its_own_events_once(start, ingest):
    start([CHECKOUT], flush_interval=60)
    checkout(0)  # buffered in the parent, which has a running thread
    pid = os.fork()
    if pid == 0:
        try:
            checkout(1)
            ok = rowfire.flush(timeout=3)
            os._exit(0 if ok else 1)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    rowfire.flush()
    numbers = sorted(e["data"]["params.n"] for e in ingest.events)
    assert numbers == [0, 1]  # the child did not resend what the parent buffered


def test_subscriptions_come_from_rowfire_and_are_cached(tmp_path):
    config = {"version": "v1", "subscriptions": [CHECKOUT]}
    with FakeIngest(config=config) as ingest:
        client = rowfire.init(
            ingest.url,
            "k",
            spool_dir=str(tmp_path),
            flush_interval=0.02,
            flush_jitter=0.0,
            config_interval=0.05,
        )
        assert _eventually(lambda: client.matcher.version == "v1")
        checkout(1)
        assert ingest.wait_for(1)
        assert ingest.events[0]["config_version"] == "v1"

        ingest.config = {"version": "v2", "subscriptions": []}
        assert _eventually(lambda: client.matcher.version == "v2")
        rowfire.close(timeout=0.5)

    # Rowfire unreachable at startup: the cached copy applies at once.
    client = rowfire.init(unused_url(), "k", spool_dir=str(tmp_path))
    try:
        assert client.matcher.version == "v2"
    finally:
        rowfire.close(timeout=0.2)


def test_nothing_happens_before_init():
    rowfire.close()
    checkout(1)
    assert rowfire.flush() is True
    assert rowfire.stats() == {}


def _eventually(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _dead_pid() -> int:
    pid = 4_000_000
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


def test_bodies_are_gzipped_unless_compression_is_off(start, ingest):
    start([CHECKOUT], catalog=False)
    checkout(1)
    assert rowfire.flush()
    start([CHECKOUT], catalog=False, compress=False)
    checkout(2)
    assert rowfire.flush()
    assert ingest.encodings == ["gzip", None]
    assert [e["data"]["params.n"] for e in ingest.events] == [1, 2]


def test_an_unusable_spool_dir_falls_back_to_memory_instead_of_failing(tmp_path, start, ingest):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)  # readable, not writable
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as a user that ignores permissions (root)")
        client = start([CHECKOUT], spool_dir=str(locked / "spool"))
        assert client.stats()["spool"] == "MemorySpool"
        checkout(1)
        assert rowfire.flush()
        assert len(ingest.events) == 1
    finally:
        locked.chmod(0o700)


def test_a_spool_dir_on_a_file_falls_back_to_memory(tmp_path, start, ingest):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    client = start([CHECKOUT], spool_dir=str(not_a_dir / "spool"))
    assert client.stats()["spool"] == "MemorySpool"
