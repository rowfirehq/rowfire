"""A local stand-in for Rowfire's ingest API, for tests, demos and benchmarks.

    with FakeIngest() as ingest:
        rowfire.init(ingest.url, "test-key", subscriptions=[...])
        ...
        rowfire.flush()
        assert ingest.events[0]["subscription"] == "goodwill_refund"

Catalog snapshots go to `catalog` (the latest one). `fail_next(503,
retry_after=1)` scripts the next responses; `delay` slows every
answer down, to show that a slow Rowfire never slows the app.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeIngest:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.batches: list[dict[str, Any]] = []
        # Latest catalog snapshot per SDK instance, as Rowfire would keep it.
        self.catalogs: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.encodings: list[str | None] = []  # Content-Encoding of each POST
        self.delay = 0.0
        self.config = config
        self._scripted: dict[str, deque[tuple[int, dict[str, str]]]] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e for b in self.batches for e in b["events"]]

    @property
    def unique_events(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for e in self.events:
            seen.setdefault(e["id"], e)
        return list(seen.values())

    @property
    def catalog(self) -> dict[str, Any]:
        """The most recent catalog snapshot (tests run one instance)."""
        with self._lock:
            if not self.catalogs:
                return {}
            return list(self.catalogs.values())[-1]

    @property
    def lost(self) -> dict[str, int]:
        total: dict[str, int] = {}
        with self._lock:
            for b in self.batches:
                for reason, n in b.get("lost", {}).items():
                    total[reason] = total.get(reason, 0) + n
        return total

    def fail_next(
        self,
        status: int,
        times: int = 1,
        retry_after: float | None = None,
        path: str = "/v1/events",
    ) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        with self._lock:
            for _ in range(times):
                self._scripted.setdefault(path, deque()).append((status, headers))

    def wait_for(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.unique_events) >= n:
                return True
            time.sleep(0.01)
        return False

    def start(self) -> FakeIngest:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeIngest:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def _handler(self) -> type:
        ingest = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path != "/v1/config" or ingest.config is None:
                    self._reply(404)
                    return
                body = json.dumps(ingest.config).encode()
                etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
                if self.headers.get("If-None-Match") == etag:
                    self._reply(304, extra={"ETag": etag})
                    return
                self._reply(200, body, {"ETag": etag, "Content-Type": "application/json"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                if ingest.delay:
                    time.sleep(ingest.delay)
                with ingest._lock:
                    ingest.requests += 1
                    queue = ingest._scripted.get(self.path)
                    scripted = queue.popleft() if queue else None
                if scripted is not None:
                    self._reply(scripted[0], extra=scripted[1])
                    return
                with ingest._lock:
                    ingest.encodings.append(self.headers.get("Content-Encoding"))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw)
                with ingest._lock:
                    if self.path == "/v1/catalog":
                        instance = payload["instance"]["id"]
                        ingest.catalogs.pop(instance, None)
                        ingest.catalogs[instance] = payload
                    else:
                        ingest.batches.append(payload)
                self._reply(202)

            def _reply(
                self, status: int, body: bytes = b"", extra: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        return Handler
