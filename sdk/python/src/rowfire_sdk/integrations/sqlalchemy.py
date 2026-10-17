"""SQLAlchemy ORM: tag each flushed write with the current operation, and
release the resulting events only when the transaction commits.

    from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy
    rowfire_sqlalchemy.instrument()          # every Session
    rowfire_sqlalchemy.instrument(MySession) # or one sessionmaker / Session class

It listens on the session's own events, as Sentry's SQLAlchemy integration
listens on the engine's, so nothing is patched:

- `after_flush` sees what was written, with each attribute's original value
  still known. Each write is matched against subscriptions and read for the
  catalog (which samples); both results are held on the session.
- `after_commit` hands the events to the worker and the writes to the catalog.
- `after_rollback` discards both, so a refund that rolled back is neither
  announced nor counted.

Not covered, in this prototype: Core statements (`session.execute(update(...))`)
bypass the ORM's unit of work, so they are not seen; and an event matched inside
a SAVEPOINT that is rolled back while the outer transaction commits is still
sent.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.base import NO_VALUE

import rowfire_sdk
from rowfire_sdk._client import Change, Client
from rowfire_sdk._context import Operation, current

log = logging.getLogger("rowfire_sdk")

_PENDING = "_rowfire_pending"
_CATALOG = "_rowfire_catalog"


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
        watched = client.watches_tables(op)
        if not watched and client.catalog is None:
            return
        events: list[dict[str, Any]] = []
        records: list[Any] = []
        for kind, objects in (
            ("insert", session.new),
            ("update", session.dirty),
            ("delete", session.deleted),
        ):
            for obj in objects:
                change = _read(kind, obj)
                if change is None:
                    continue
                record = client.read_write(op, change)
                if record is not None:
                    records.append(record)
                if watched:
                    events.extend(_match(client, op, change))
        if events:
            session.info.setdefault(_PENDING, []).extend(events)
        if records:
            session.info.setdefault(_CATALOG, []).extend(records)
    except Exception:
        client.counters.lose("internal_error")
        log.exception("rowfire-sdk: could not read a flush")


def _read(kind: str, obj: Any) -> Change | None:
    """Describe one flushed object as a `Change`, or None if nothing changed."""
    state = inspect(obj)
    mapper = state.mapper
    columns = _column_keys(mapper)
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
            return None  # dirty only in a relationship, or set to the same value

    def row(key: str) -> Any:
        return values.get(key)

    def old(key: str) -> Any:
        if kind == "insert":
            return None
        return before[key] if key in before else values.get(key)

    def changed() -> Any:
        if kind == "insert":
            return {key for key in columns if values.get(key) is not None}
        return before.keys()

    return Change(
        mapper.local_table.name,
        kind,
        row,
        old,
        changed,
        columns=columns,
        describe=lambda: describe(mapper),
    )


def _match(client: Client, op: Operation | None, change: Change) -> list[dict[str, Any]]:
    subs = client.subscriptions_for(op, change.table)
    if not subs:
        return []
    return client.match_change(op, subs, change)


def describe(mapper: Any) -> dict[str, dict[str, Any]]:
    """Each column's declared type, from the model: no data involved."""
    columns: dict[str, dict[str, Any]] = {}
    for prop in mapper.column_attrs:
        column = prop.columns[0]
        info: dict[str, Any] = {
            "type": type(column.type).__name__.lower(),
            "nullable": bool(getattr(column, "nullable", True)),
            "primary_key": bool(getattr(column, "primary_key", False)),
        }
        enums = getattr(column.type, "enums", None)
        if enums:
            info["declared_values"] = list(enums)
        references = [fk.target_fullname for fk in getattr(column, "foreign_keys", ())]
        if references:
            info["references"] = references
        columns[prop.key] = info
    return columns


_columns_by_mapper: dict[Any, frozenset] = {}


def _column_keys(mapper: Any) -> frozenset:
    keys = _columns_by_mapper.get(mapper)
    if keys is None:
        keys = frozenset(c.key for c in mapper.column_attrs)
        _columns_by_mapper[mapper] = keys
    return keys


def _after_commit(session: Session) -> None:
    events = session.info.pop(_PENDING, None)
    records = session.info.pop(_CATALOG, None)
    client = rowfire_sdk.get_client()
    if client is None:
        return
    if events:
        client.emit(events)
    if records:
        client.record_writes(records)


def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)
    session.info.pop(_CATALOG, None)
