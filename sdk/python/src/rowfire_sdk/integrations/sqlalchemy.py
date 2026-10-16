"""SQLAlchemy ORM: tag each flushed write with the current operation, and
release the resulting events only when the transaction commits.

    from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
    rowfire_sqlalchemy.instrument()          # every Session
    rowfire_sqlalchemy.instrument(MySession) # or one sessionmaker / Session class

It listens on the session's own events, as Sentry's SQLAlchemy integration
listens on the engine's, so nothing is patched:

- `after_flush` sees what was written, with each attribute's history still
  intact. Matching happens there, while the old values are known; matched
  events are held on the session.
- `after_commit` hands them to the worker.
- `after_rollback` discards them, so a refund that rolled back is never
  announced.

Not covered, in this prototype: Core statements (`session.execute(update(...))`)
bypass the ORM's unit of work, so they are not seen; and an event matched inside
a SAVEPOINT that is rolled back while the outer transaction commits is still
sent.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.base import NO_VALUE

import rowfire_sdk
from rowfire_sdk._client import Change, Client
from rowfire_sdk._context import Operation, current

log = logging.getLogger("rowfire_sdk")

_PENDING = "_rowfire_pending"


def instrument(target: Any = Session) -> None:
    if event.contains(target, "after_flush", _after_flush):
        return
    event.listen(target, "after_flush", _after_flush)
    event.listen(target, "after_commit", _after_commit)
    event.listen(target, "after_rollback", _after_rollback)


def uninstrument(target: Any = Session) -> None:
    for name, fn in (
        ("after_flush", _after_flush),
        ("after_commit", _after_commit),
        ("after_rollback", _after_rollback),
    ):
        if event.contains(target, name, fn):
            event.remove(target, name, fn)


def _after_flush(session: Session, flush_context: Any) -> None:
    client = rowfire_sdk.get_client()
    if client is None:
        return
    try:
        op = current.get()
        if not client.watches_tables(op):
            return
        events: list[dict[str, Any]] = []
        for kind, objects in (
            ("insert", session.new),
            ("update", session.dirty),
            ("delete", session.deleted),
        ):
            for obj in objects:
                events.extend(_match(client, op, kind, obj))
        if events:
            session.info.setdefault(_PENDING, []).extend(events)
    except Exception:
        client.counters.lose("internal_error")
        log.exception("rowfire-sdk: could not read a flush")


def _match(client: Client, op: Operation | None, kind: str, obj: Any) -> list[dict[str, Any]]:
    state = inspect(obj)
    table = state.mapper.local_table.name
    subs = client.subscriptions_for(op, table)
    if not subs:
        return []
    columns = _column_keys(state.mapper)
    values = state.dict  # read without triggering a lazy load inside the flush

    # `committed_state` holds the value each attribute had before this unit of
    # work changed it: one dict, where asking every attribute for its history
    # builds an object per column. It also lists attributes set to the value
    # they already had, so those are filtered out here.
    before: dict[str, Any] = {}
    if kind == "update":
        for key, was in state.committed_state.items():
            if key not in columns:
                continue
            if was is NO_VALUE:
                before[key] = None  # not loaded before it was set: unknown
            elif was != values.get(key):
                before[key] = was
        if not before:
            return []  # dirty only in a relationship, or set to the same value

    def row(key: str) -> Any:
        return values.get(key)

    def old(key: str) -> Any:
        if kind == "insert":
            return None
        return before[key] if key in before else values.get(key)

    def changed() -> Collection[str]:
        if kind == "insert":
            return {key for key in columns if values.get(key) is not None}
        return before.keys()

    return client.match_change(op, subs, Change(table, kind, row, old, changed))


_columns_by_mapper: dict[Any, frozenset] = {}


def _column_keys(mapper: Any) -> frozenset:
    keys = _columns_by_mapper.get(mapper)
    if keys is None:
        keys = frozenset(c.key for c in mapper.column_attrs)
        _columns_by_mapper[mapper] = keys
    return keys


def _after_commit(session: Session) -> None:
    events = session.info.pop(_PENDING, None)
    client = rowfire_sdk.get_client()
    if events and client is not None:
        client.emit(events)


def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)
