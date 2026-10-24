"""Subscriptions, compiled so the request thread does as little as possible.

A subscription says which operation it listens to, optionally which table
change inside it, which conditions must hold, and which fields to send:

    {
      "id": "goodwill_refund",
      "operation": "POST /admin/charges/{id}/refund",
      "write": {"table": "charges", "kind": ["update"], "changed": "status"},
      "where": {"row.status": "refunded", "actor.role": "support"},
      "fields": ["row.id", "row.account_id", "row.amount", "actor.id"]
    }

Without `write`, it fires when the operation completes successfully.

Field paths name where a value comes from: `row.<column>` (the row after the
change), `old.<column>` (its value before), `actor.<attr>`, `params.<name>`
(path parameters), `operation` and `response.status`.

The ceiling is the app's side of the contract. It is set in the app's own code,
never by config, and no subscription can reach past it: an operation outside
`allow_operations` matches nothing, and a subscription that asks for or tests
a denied field is rejected outright. Testing one counts too, because an event
that fires only when `row.email` ends in `@bigcorp.com` says something about
that email.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

NAMESPACES = ("row", "old", "actor", "params")
SCALARS = ("operation", "response.status")
KINDS = frozenset({"insert", "update", "delete"})

MISSING = object()


class SubscriptionError(ValueError):
    pass


@dataclass(frozen=True)
class Ceiling:
    """What no subscription may ever reach, set in the app's code."""

    # Operation names or globs ("POST /admin/*"). None allows every operation.
    allow_operations: Sequence[str] | None = None
    # Field globs. "*email*" matches "row.email" and "actor.email"; a pattern
    # without a dot is tried against the last segment as well as the whole path.
    deny_fields: Sequence[str] = ()

    def allows_operation(self, name: str) -> bool:
        if self.allow_operations is None:
            return True
        return any(fnmatch.fnmatchcase(name, p) for p in self.allow_operations)

    def denies_field(self, path: str) -> bool:
        lowered = path.lower()
        leaf = lowered.rsplit(".", 1)[-1]
        for pattern in self.deny_fields:
            p = pattern.lower()
            if fnmatch.fnmatchcase(lowered, p):
                return True
            if "." not in p and fnmatch.fnmatchcase(leaf, p):
                return True
        return False


@dataclass(frozen=True)
class Subscription:
    id: str
    operation: str
    table: str | None
    kinds: frozenset
    changed: str | None
    where: tuple[tuple[str, Any], ...]
    fields: tuple[str, ...]

    @property
    def is_glob(self) -> bool:
        return any(c in self.operation for c in "*?[")

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Subscription:
        try:
            sub_id = str(raw["id"])
            operation = str(raw["operation"])
        except KeyError as exc:
            raise SubscriptionError(f"missing {exc.args[0]!r}") from None
        # `write`, not `on`: in YAML 1.1 a bare `on:` key reads as `true`, and
        # these are written in Rowfire's YAML definitions. `on` still works.
        on = raw.get("write") or raw.get("on") or {}
        table = on.get("table")
        kinds = on.get("kind", ["insert", "update", "delete"] if table else [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if not set(kinds) <= KINDS:
            raise SubscriptionError(f"{sub_id}: unknown kind in {kinds!r}")
        if on and not table:
            raise SubscriptionError(f"{sub_id}: 'write' needs a table")
        where = raw.get("where") or {}
        fields = list(raw.get("fields") or [])
        for path in [*where, *fields]:
            _check_path(sub_id, path, table is not None)
        return cls(
            id=sub_id,
            operation=operation,
            table=table,
            kinds=frozenset(kinds),
            changed=on.get("changed"),
            where=tuple(where.items()),
            fields=tuple(fields),
        )


def _check_path(sub_id: str, path: str, is_change: bool) -> None:
    if path in SCALARS:
        return
    namespace, _, rest = path.partition(".")
    if namespace not in NAMESPACES or not rest:
        raise SubscriptionError(f"{sub_id}: unknown field {path!r}")
    if namespace in ("row", "old") and not is_change:
        raise SubscriptionError(f"{sub_id}: {path!r} needs a 'write' table")


class Facts:
    """What one observation knows, looked up by field path."""

    __slots__ = ("operation", "actor", "params", "status", "row", "old")

    def __init__(
        self,
        operation: str,
        actor: Mapping[str, Any] | None,
        params: Mapping[str, Any] | None,
        status: int | None = None,
        row: Callable[[str], Any] | None = None,
        old: Callable[[str], Any] | None = None,
    ) -> None:
        self.operation = operation
        self.actor = actor or {}
        self.params = params or {}
        self.status = status
        self.row = row
        self.old = old

    def get(self, path: str) -> Any:
        if path == "operation":
            return self.operation
        if path == "response.status":
            return self.status
        namespace, _, key = path.partition(".")
        if namespace == "actor":
            return self.actor.get(key, MISSING)
        if namespace == "params":
            return self.params.get(key, MISSING)
        if namespace == "row" and self.row is not None:
            return self.row(key)
        if namespace == "old" and self.old is not None:
            return self.old(key)
        return MISSING


def evaluate(sub: Subscription, facts: Facts) -> dict[str, Any] | None:
    """The fields to send if `sub` matches, else None."""
    for path, expected in sub.where:
        value = facts.get(path)
        if isinstance(expected, list):
            if value not in expected:
                return None
        elif value != expected:
            return None
    data = {}
    for path in sub.fields:
        value = facts.get(path)
        data[path] = None if value is MISSING else plain(value)
    return data


def plain(value: Any) -> Any:
    """A JSON-ready copy, so a later change to the app's object cannot reach us."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return plain(value.value)
    if isinstance(value, Decimal):
        # A number, like the float the app may have set before the database
        # handed back a Decimal, unless a float would lose digits (then the
        # exact string). 12.10 becomes 12.1; 0.1000000000000000055 stays text.
        if value.is_finite() and Decimal(repr(float(value))) == value:
            return int(value) if value == value.to_integral_value() else float(value)
        return str(value)
    return str(value)


class Resolved:
    """The subscriptions that apply to one operation name."""

    __slots__ = ("tables", "on_complete")

    def __init__(self) -> None:
        self.tables: dict[str, list[Subscription]] = {}
        self.on_complete: list[Subscription] = []

    def __bool__(self) -> bool:
        return bool(self.tables or self.on_complete)


_NOTHING = Resolved()


class Matcher:
    """Compiled subscriptions, resolved per operation name and cached.

    Most operations have no subscription, so the common case is one dict
    lookup that returns an empty `Resolved`.
    """

    CACHE_LIMIT = 10_000

    def __init__(
        self,
        subscriptions: Sequence[Mapping[str, Any]],
        ceiling: Ceiling,
        version: str | None = None,
    ) -> None:
        self.version = version
        self.ceiling = ceiling
        self.rejected: list[tuple[str, str]] = []
        self._exact: dict[str, list[Subscription]] = {}
        self._globs: list[Subscription] = []
        self._cache: dict[str, Resolved] = {}
        for raw in subscriptions:
            try:
                sub = Subscription.parse(raw)
            except SubscriptionError as exc:
                self.rejected.append((str(raw.get("id", "?")), str(exc)))
                continue
            denied = [p for p, _ in sub.where if ceiling.denies_field(p)]
            denied += [p for p in sub.fields if ceiling.denies_field(p)]
            if denied:
                self.rejected.append((sub.id, f"denied fields: {', '.join(denied)}"))
                continue
            if sub.is_glob:
                self._globs.append(sub)
            else:
                self._exact.setdefault(sub.operation, []).append(sub)

    def resolve(self, operation: str) -> Resolved:
        cached = self._cache.get(operation)
        if cached is not None:
            return cached
        resolved = self._build(operation)
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[operation] = resolved
        return resolved

    def _build(self, operation: str) -> Resolved:
        if not self.ceiling.allows_operation(operation):
            return _NOTHING
        subs = list(self._exact.get(operation, ()))
        subs += [s for s in self._globs if fnmatch.fnmatchcase(operation, s.operation)]
        if not subs:
            return _NOTHING
        resolved = Resolved()
        for sub in subs:
            if sub.table is None:
                resolved.on_complete.append(sub)
            else:
                resolved.tables.setdefault(sub.table, []).append(sub)
        return resolved
