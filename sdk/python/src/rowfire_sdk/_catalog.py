"""The catalog: what exists in the app and what happens in it, for the dashboard.

Subscriptions need names someone can pick from: which operations exist, which
tables each one writes, which columns change, and which values those columns
take. The catalog collects that inside the app and sends it to Rowfire every
`interval` seconds, so a person can build a subscription from a list instead of
from knowledge of the code.

Three sources, cheapest first:

- declared: routes the app's router knows (before anyone calls them) and each
  table's columns, types and enum values, from the ORM's models;
- counted: every operation and every write, by operation, table and kind;
- observed: on a sample of writes, the values each column takes and, for
  updates, which value it changed from and to.

Observed values are real data. Nothing restricts them yet beyond the ceiling's
`deny_fields`, if one is set. Two settings keep the catalog useful rather than
private: a column that turns out to have more than `max_values` distinct values
(ids, emails, amounts) is marked `many_values` and its list is dropped, since a
picker of twenty random emails helps nobody; and at most `samples_per_minute`
writes per operation, table and kind are inspected, so a hot path pays for
discovery a bounded number of times a minute. Every write is still counted.

Counts are cumulative since the process started. Rowfire keeps the latest
snapshot per instance and adds instances up, so a lost send loses nothing.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from rowfire_sdk._matching import Ceiling, plain

WINDOW = 60.0  # seconds per sampling window


class Values:
    """Distinct values of one column or attribute, until there are too many."""

    __slots__ = ("seen", "many")

    def __init__(self) -> None:
        self.seen: dict[Any, int] = {}
        self.many = False

    def add(self, value: Any, limit: int) -> None:
        if self.many:
            return
        if not isinstance(value, tuple):  # a transition arrives already plain
            value = plain(value)
        if value in self.seen:
            self.seen[value] += 1
        elif len(self.seen) >= limit:
            self.many = True
            self.seen = {}
        else:
            self.seen[value] = 1

    def export(self) -> dict[str, Any]:
        if self.many:
            return {"many_values": True}
        return {"values": [{"value": v, "seen": n} for v, n in self.seen.items()]}


class _Operation:
    __slots__ = ("declared", "seen", "errors", "statuses", "params", "actor")

    def __init__(self) -> None:
        self.declared = False
        self.seen = 0
        self.errors = 0
        self.statuses: dict[int, int] = {}
        self.params: dict[str, Values] = {}
        self.actor: dict[str, Values] = {}


class _Write:
    __slots__ = ("seen", "sampled", "changed")

    def __init__(self) -> None:
        self.seen = 0
        self.sampled = 0
        self.changed: dict[str, int] = {}


class _Table:
    __slots__ = ("columns", "values", "transitions")

    def __init__(self, columns: dict[str, dict[str, Any]]) -> None:
        self.columns = columns
        self.values: dict[str, Values] = {}
        self.transitions: dict[str, Values] = {}


class Catalog:
    def __init__(
        self,
        ceiling: Ceiling,
        *,
        samples_per_minute: int = 100,
        max_values: int = 20,
    ) -> None:
        self._ceiling = ceiling
        self._samples_per_minute = samples_per_minute
        self._max_values = max_values
        self._lock = threading.Lock()
        self._reset()
        self.instance_id = os.urandom(8).hex()
        self.started_at = datetime.now(timezone.utc).isoformat()

    def _reset(self) -> None:
        self._operations: dict[str, _Operation] = {}
        self._writes: dict[tuple[str, str, str], _Write] = {}
        self._tables: dict[str, _Table] = {}
        self._window_start = time.monotonic()
        self._window: dict[tuple[str, str, str], int] = {}
        self.dirty = False

    def after_fork(self) -> None:
        """A child process is its own instance, starting from nothing."""
        self._lock = threading.Lock()
        self._reset()
        self.instance_id = os.urandom(8).hex()

    # -- request thread -------------------------------------------------------

    def declare_operations(self, names: list[str]) -> None:
        with self._lock:
            for name in names:
                self._operation(name).declared = True
            self.dirty = True

    def observe_operation(
        self,
        name: str,
        status: int | None,
        failed: bool,
        params: dict[str, Any],
        actor: dict[str, Any] | None,
    ) -> None:
        limit = self._max_values
        with self._lock:
            op = self._operation(name)
            op.seen += 1
            if failed or (status is not None and status >= 500):
                op.errors += 1
            if status is not None:
                op.statuses[status] = op.statuses.get(status, 0) + 1
            for key, value in params.items():
                _values(op.params, key).add(value, limit)
            for key, value in (actor or {}).items():
                if not self._ceiling.denies_field(f"actor.{key}"):
                    _values(op.actor, key).add(value, limit)
            self.dirty = True

    def read_write(self, operation: str, change: Any) -> tuple[Any, ...]:
        """At flush: decide whether to sample this write, and if so read what
        the sample needs while the row is still in memory. Nothing is counted
        yet; `record_writes` does that once the transaction commits, so a
        write that rolls back never shows up in the catalog.

        `change` is a `Change` whose `columns` and `describe` are set.
        """
        key = (operation, change.table, change.kind)
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= WINDOW:
                self._window_start = now
                self._window = {}
            taken = self._window.get(key, 0)
            if taken >= self._samples_per_minute:
                return (key, None)
            self._window[key] = taken + 1
            known = change.table in self._tables

        columns = None if known else change.describe()
        changed = list(change.changed())
        values = {c: change.row(c) for c in change.columns if not self._denied(c)}
        moves = {}
        if change.kind == "update":
            moves = {c: (change.old(c), change.row(c)) for c in changed if not self._denied(c)}
        return (key, (columns, changed, values, moves))

    def record_writes(self, records: list[tuple[Any, ...]]) -> None:
        """At commit: count the writes and merge what their samples read."""
        limit = self._max_values
        with self._lock:
            for key, sample in records:
                write = self._writes.get(key)
                if write is None:
                    write = self._writes[key] = _Write()
                write.seen += 1
                if sample is None:
                    continue
                write.sampled += 1
                columns, changed, values, moves = sample
                table = self._tables.get(key[1])
                if table is None:
                    for name, info in (columns or {}).items():
                        if self._denied(name):
                            info["denied"] = True
                    table = self._tables[key[1]] = _Table(columns or {})
                for column in changed:
                    write.changed[column] = write.changed.get(column, 0) + 1
                for column, value in values.items():
                    _values(table.values, column).add(value, limit)
                for column, (was, now) in moves.items():
                    _values(table.transitions, column).add((plain(was), plain(now)), limit)
            self.dirty = True

    # -- background thread ----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            operations: dict[str, Any] = {}
            for name, op in self._operations.items():
                operations[name] = {
                    "declared": op.declared,
                    "seen": op.seen,
                    "errors": op.errors,
                    "statuses": {str(s): n for s, n in op.statuses.items()},
                    "params": {k: v.export() for k, v in op.params.items()},
                    "actor": {k: v.export() for k, v in op.actor.items()},
                    "writes": [],
                }
            for (name, table, kind), write in self._writes.items():
                entry = operations.setdefault(name, {"declared": False, "seen": 0, "writes": []})
                entry["writes"].append(
                    {
                        "table": table,
                        "kind": kind,
                        "seen": write.seen,
                        "sampled": write.sampled,
                        "changed": dict(write.changed),
                    }
                )
            tables: dict[str, Any] = {}
            for name, table in self._tables.items():
                columns = {}
                for column, info in table.columns.items():
                    observed = table.values.get(column)
                    columns[column] = {**info, **(observed.export() if observed else {})}
                transitions = {}
                for column, moves in table.transitions.items():
                    if moves.many:
                        transitions[column] = {"many_values": True}
                    else:
                        transitions[column] = {
                            "values": [
                                {"from": was, "to": now, "seen": n}
                                for (was, now), n in moves.seen.items()
                            ]
                        }
                tables[name] = {"columns": columns, "transitions": transitions}
            self.dirty = False
        return {
            "instance": {"id": self.instance_id, "started_at": self.started_at},
            "operations": operations,
            "tables": tables,
        }

    # -- helpers ------------------------------------------------------------------

    def _operation(self, name: str) -> _Operation:
        op = self._operations.get(name)
        if op is None:
            op = self._operations[name] = _Operation()
        return op

    def _denied(self, column: str) -> bool:
        return self._ceiling.denies_field(f"row.{column}")


def _values(store: dict[str, Values], key: str) -> Values:
    values = store.get(key)
    if values is None:
        values = store[key] = Values()
    return values
