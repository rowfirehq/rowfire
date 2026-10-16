"""The client: subscriptions in, events out.

Integrations call two things on the request thread. `subscriptions_for()` is
the cheap question asked first ("does anything listen to this table in this
operation?"), and is usually answered by one dict lookup. `match_change()` and
`operation_finished()` build events only when something does.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Callable

from rowfire_sdk._context import NO_OPERATION, Operation
from rowfire_sdk._matching import Ceiling, Facts, Matcher, Subscription, evaluate
from rowfire_sdk._spool import DiskSpool, MemorySpool
from rowfire_sdk._transport import Transport
from rowfire_sdk._worker import Counters, Worker

log = logging.getLogger("rowfire_sdk")


class Change:
    """One row written inside an operation, read lazily.

    `row` and `old` look a column up; `changed` lists the columns this write
    set. All three are only called once some subscription listens to the
    table, so an unwatched write costs nothing beyond the table lookup.
    """

    __slots__ = ("table", "kind", "row", "old", "changed")

    def __init__(
        self,
        table: str,
        kind: str,
        row: Callable[[str], Any],
        old: Callable[[str], Any],
        changed: Callable[[], Collection[str]],
    ) -> None:
        self.table = table
        self.kind = kind
        self.row = row
        self.old = old
        self.changed = changed


class Client:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        subscriptions: Sequence[Mapping[str, Any]] | None = None,
        ceiling: Ceiling | None = None,
        spool_dir: str | None = None,
        spool_max_bytes: int = 64 * 1024 * 1024,
        batch_size: int = 200,
        max_queue: int = 10_000,
        flush_interval: float = 1.0,
        config_interval: float = 30.0,
        timeout: float = 5.0,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
    ) -> None:
        self.ceiling = ceiling or Ceiling()
        self.counters = Counters()
        self.config_errors = 0
        self._transport = Transport(endpoint, api_key, timeout)
        if spool_dir:
            self._spool: Any = DiskSpool(spool_dir, spool_max_bytes)
            self._config_cache: str | None = os.path.join(spool_dir, "config.json")
        else:
            self._spool = MemorySpool(spool_max_bytes)
            self._config_cache = None
        self._etag: str | None = None
        self._config_interval = config_interval
        self._next_config = 0.0
        self.matcher = Matcher([], self.ceiling)
        # Subscriptions given in code are fixed; otherwise they come from Rowfire,
        # starting from the last copy cached on disk so a restart does not wait.
        static = subscriptions is not None
        if static:
            self.set_subscriptions(subscriptions)
        else:
            self._load_cached_config()
        self.worker = Worker(
            self._transport,
            self._spool,
            self.counters,
            batch_size=batch_size,
            max_queue=max_queue,
            flush_interval=flush_interval,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            on_tick=None if static else self._poll_config,
        )
        # Otherwise the thread starts with the first event, as Sentry's does. It
        # is needed sooner to poll for config, or to send what an earlier
        # process left in the spool.
        if not static or self._spool.pending():
            self.worker.ensure_thread()

    # -- subscriptions ----------------------------------------------------------

    def set_subscriptions(
        self, subscriptions: Sequence[Mapping[str, Any]], version: str | None = None
    ) -> None:
        matcher = Matcher(subscriptions, self.ceiling, version)
        for sub_id, reason in matcher.rejected:
            log.warning("rowfire-sdk: subscription %s rejected: %s", sub_id, reason)
        # One assignment, so the request thread sees the old set or the new one.
        self.matcher = matcher

    def _poll_config(self) -> None:
        now = time.monotonic()
        if now < self._next_config:
            return
        self._next_config = now + self._config_interval
        try:
            status, etag, config = self._transport.fetch_config(self._etag)
        except (OSError, http.client.HTTPException, ValueError):
            self.config_errors += 1
            return
        if config is None:
            if status != 304:
                self.config_errors += 1
            return
        self._etag = etag
        self.set_subscriptions(config.get("subscriptions", []), config.get("version"))
        self._write_cached_config(config)

    def _load_cached_config(self) -> None:
        if not self._config_cache:
            return
        try:
            with open(self._config_cache) as f:
                config = json.load(f)
        except (OSError, ValueError):
            return
        self.set_subscriptions(config.get("subscriptions", []), config.get("version"))

    def _write_cached_config(self, config: dict[str, Any]) -> None:
        if not self._config_cache:
            return
        tmp = f"{self._config_cache}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(config, f)
            os.replace(tmp, self._config_cache)
        except OSError:
            log.warning("rowfire-sdk: cannot cache the config", exc_info=True)

    # -- request thread -------------------------------------------------------

    def subscriptions_for(self, op: Operation | None, table: str) -> list[Subscription]:
        return self.matcher.resolve(op.name if op else NO_OPERATION).tables.get(table, [])

    def watches_tables(self, op: Operation | None) -> bool:
        return bool(self.matcher.resolve(op.name if op else NO_OPERATION).tables)

    def match_change(
        self, op: Operation | None, subs: list[Subscription], change: Change
    ) -> list[dict[str, Any]]:
        name = op.name if op else NO_OPERATION
        facts = Facts(
            name,
            op.actor if op else None,
            op.params if op else None,
            op.status if op else None,
            row=change.row,
            old=change.old,
        )
        changed: Collection[str] | None = None
        events = []
        for sub in subs:
            if change.kind not in sub.kinds:
                continue
            if sub.changed is not None:
                if changed is None:
                    changed = change.changed()
                if sub.changed not in changed:
                    continue
            data = evaluate(sub, facts)
            if data is not None:
                events.append(self._event(sub, name, data))
        return events

    def operation_finished(self, op: Operation) -> None:
        if op.status is not None and op.status >= 400:
            return
        resolved = self.matcher.resolve(op.name)
        if not resolved.on_complete:
            return
        facts = Facts(op.name, op.actor, op.params, op.status)
        events = []
        for sub in resolved.on_complete:
            data = evaluate(sub, facts)
            if data is not None:
                events.append(self._event(sub, op.name, data))
        self.emit(events)

    def emit(self, events: list[dict[str, Any]]) -> None:
        if events:
            self.worker.add(events)

    def _event(self, sub: Subscription, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            # Rowfire keeps the first copy of an id, so a batch resent after a
            # timeout that had in fact arrived cannot fire twice. 128 random
            # bits, like a uuid4, at a fifth of the cost of building one.
            "id": os.urandom(16).hex(),
            "subscription": sub.id,
            "config_version": self.matcher.version,
            "operation": operation,
            # A float here; the background thread formats it.
            "occurred_at": time.time(),
            "data": data,
        }

    # -- lifecycle --------------------------------------------------------------

    def flush(self, timeout: float = 2.0) -> bool:
        return self.worker.flush(timeout)

    def close(self, timeout: float = 2.0) -> None:
        self.worker.close(timeout)

    def stats(self) -> dict[str, Any]:
        return {
            "queued": self.worker.queued(),
            "sent": self.counters.sent,
            "spooled": self.counters.spooled,
            "waiting_in_spool": self._spool.pending(),
            "lost": dict(self.counters.lost_total),
            "config_version": self.matcher.version,
            "config_errors": self.config_errors,
            "rejected_subscriptions": list(self.matcher.rejected),
        }
