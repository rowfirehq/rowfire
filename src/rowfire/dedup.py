"""Dedup policies as pure functions over row lists.

Deliberately not done in SQL: the live path replaces this with a lookup
against the persistent fire ledger without touching compile.py.

The two halves come from different places, which is the point of the split.
The **key** belongs to the trigger -- "one row per order, identified by id" is
a fact about the query, and only its author reliably knows it. The **policy**
belongs to the rule -- once ever, once a week -- because the same query wants
different answers for different actions.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from .definitions import DedupPolicy, Period, Rule, Trigger

Row = dict[str, Any]


def apply(rows: Sequence[Row], trigger: Trigger, rule: Rule) -> list[Row]:
    """Reduce the trigger's rows to the fires this rule would have produced.

    Input order is not trusted; rows are sorted by event_time first so
    "earliest wins" is well defined regardless of what the query returned.
    """
    if not rows:
        return []

    event_time_column = trigger.event_time
    ordered = sorted(rows, key=lambda r: _sort_key(r, event_time_column))

    if rule.policy is DedupPolicy.once_ever:
        return _once_ever(ordered, trigger.key)
    if rule.policy is DedupPolicy.once_per_period:
        assert rule.period is not None  # guaranteed by Rule's validator
        return _once_per_period(ordered, trigger.key, rule.period, event_time_column)
    if rule.policy is DedupPolicy.once_per_n:
        assert rule.n is not None
        return _once_per_n(ordered, trigger.key, rule.n)

    raise ValueError(f"unhandled dedup policy: {rule.policy}")


def _sort_key(row: Row, event_time_column: str | None) -> tuple[int, Any]:
    """Sort by event_time, with nulls last and ties broken deterministically."""
    if not event_time_column:
        return (0, 0)
    value = row.get(event_time_column)
    if value is None:
        return (1, 0)
    return (0, value)


def dedup_key(row: Row, key_columns: Sequence[str]) -> tuple[Hashable, ...]:
    """The identity of a fire. Missing columns become None rather than raising.

    A KeyError here would be the wrong failure: definitions.py already refuses
    a dedup key that is not emitted, so a miss at this point means the row
    genuinely has no value for it.
    """
    return tuple(_hashable(row.get(column)) for column in key_columns)


def _hashable(value: Any) -> Hashable:
    if isinstance(value, (list, dict, set)):
        return repr(value)
    return value


def _once_ever(rows: Sequence[Row], key_columns: Sequence[str]) -> list[Row]:
    """One fire per key, ever. Earliest row by event_time wins."""
    seen: set[tuple[Hashable, ...]] = set()
    kept: list[Row] = []
    for row in rows:
        key = dedup_key(row, key_columns)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    return kept


def _once_per_period(
    rows: Sequence[Row],
    key_columns: Sequence[str],
    period: Period,
    event_time_column: str | None,
) -> list[Row]:
    """One fire per key per calendar bucket.

    Rows with no event_time cannot be bucketed. They are dropped here and
    counted separately upstream -- engine.py reports them explicitly.
    """
    seen: set[tuple[Any, ...]] = set()
    kept: list[Row] = []
    for row in rows:
        when = row.get(event_time_column) if event_time_column else None
        if when is None:
            continue
        bucket = period_bucket(when, period)
        key = (*dedup_key(row, key_columns), bucket)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    return kept


def _once_per_n(rows: Sequence[Row], key_columns: Sequence[str], n: int) -> list[Row]:
    """Fire on every Nth occurrence per key (the 1st, the N+1th, ...)."""
    counts: dict[tuple[Hashable, ...], int] = {}
    kept: list[Row] = []
    for row in rows:
        key = dedup_key(row, key_columns)
        index = counts.get(key, 0)
        if index % n == 0:
            kept.append(row)
        counts[key] = index + 1
    return kept


def period_bucket(when: datetime | date, period: Period) -> date:
    """Truncate a timestamp to the start of its bucket.

    Weeks start Monday, matching date_trunc('week', ...) in Postgres, so the
    v1 move of this logic into SQL does not silently shift boundaries.
    """
    day = when.date() if isinstance(when, datetime) else when

    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)

    raise ValueError(f"unknown period: {period}")
