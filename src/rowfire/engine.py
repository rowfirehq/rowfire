"""Execution and result assembly for a backtest.

Opening the customer's database -- Postgres or MySQL, read-only, with a
statement timeout -- lives in `sources.py`. This module decides what to run
against it and what the rows mean.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import dedup as dedup_module
from . import sources
from .compile import (
    CompiledQuery,
    CompileError,
    compile_null_event_time_probe,
    compile_query,
)
from .definitions import Definitions
from .sources import EngineError, SourceConnection, connect

__all__ = ["BacktestResult", "EngineError", "SourceConnection", "connect", "resolve_dsn", "run"]


@dataclass
class BacktestResult:
    rule_name: str
    trigger_name: str
    since: datetime | None
    until: datetime | None
    days: int

    matched_rows: int = 0
    fires: int = 0
    unique_keys: int = 0
    null_event_time_rows: int = 0
    # Read from the cursor, not from a hand-maintained model: the query's own
    # output is the only authority on what columns it returns.
    column_types: dict[str, str] = field(default_factory=dict)

    per_day: dict[date, int] = field(default_factory=dict)
    sample: list[dict[str, Any]] = field(default_factory=list)
    emit_columns: list[str] = field(default_factory=list)

    query_ms: float = 0.0
    timeless: bool = False
    compiled: CompiledQuery | None = None

    @property
    def dedup_removed(self) -> int:
        return max(self.matched_rows - self.fires, 0)

    @property
    def mean_per_day(self) -> float:
        if not self.per_day:
            return 0.0
        return self.fires / max(len(self.day_range()), 1)

    def day_range(self) -> list[date]:
        """Every day in the window, including days with no fires.

        Iterating observed days only would hide the zero-volume stretches,
        which are exactly what a user needs to see.
        """
        if not self.since or not self.until:
            return sorted(self.per_day)
        start, end = self.since.date(), self.until.date()
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def busiest_day(self) -> tuple[date, int] | None:
        return max(self.per_day.items(), key=lambda kv: kv[1]) if self.per_day else None

    def quietest_day(self) -> tuple[date, int] | None:
        days = self.day_range()
        if not days:
            return None
        counts = [(d, self.per_day.get(d, 0)) for d in days]
        return min(counts, key=lambda kv: kv[1])


def resolve_dsn(definitions: Definitions) -> str:
    """Read the DSN from the env var named in the definitions file.

    The DSN is never stored in the file, never logged, and never echoed --
    only the name of the variable it came from.
    """
    var = definitions.source.dsn_env
    dsn = os.environ.get(var)
    if not dsn:
        raise EngineError(
            f"environment variable {var} is not set. "
            f"Export a read-only DSN there, e.g.\n"
            f"  export {var}='postgresql://readonly@host:5432/dbname'\n"
            f"  export {var}='mysql://readonly@host:3306/dbname'"
        )
    return dsn


def run(
    definitions: Definitions,
    rule_name: str,
    days: int = 90,
    sample: int = 20,
    now: datetime | None = None,
    dsn: str | None = None,
) -> BacktestResult:
    """Execute a backtest for one rule.

    Backtest semantics, restated because it is the easiest thing to get subtly
    wrong: this evaluates rows as they are *now* and uses event_time to place
    them in the past. It does not replay history. An order completed in March
    and refunded in April looks like it never fired. The result carries the
    signals needed to say so out loud.
    """
    rule = definitions.rules.get(rule_name)
    if rule is None:
        known = ", ".join(sorted(definitions.rules)) or "none defined"
        raise CompileError([f"unknown rule `{rule_name}`. Known rules: {known}"])
    trigger = definitions.triggers[rule.trigger]

    until = now or datetime.now(UTC)
    since = until - timedelta(days=days)

    # An explicit DSN wins: callers that hold the stored connection for the
    # trigger's source pass it straight in. Falling back to the environment
    # keeps `resolve_dsn` as the one place that knows how a DSN is found when
    # nobody supplies one.
    dsn = dsn or resolve_dsn(definitions)
    dialect = sources.DIALECTS[sources.kind_of(dsn)]

    compiled = compile_query(
        trigger,
        since=since if trigger.event_time else None,
        until=until if trigger.event_time else None,
        limit=definitions.source.max_rows,
        dialect=dialect,
    )

    result = BacktestResult(
        rule_name=rule_name,
        trigger_name=rule.trigger,
        since=since if trigger.event_time else None,
        until=until if trigger.event_time else None,
        days=days,
        timeless=compiled.timeless,
        compiled=compiled,
    )

    with connect(dsn, definitions.source.statement_timeout_ms) as conn:
        started = time.perf_counter()
        rows, columns = _fetch(conn, compiled)
        result.query_ms = (time.perf_counter() - started) * 1000

        probe = compile_null_event_time_probe(trigger, dialect=dialect)
        if probe is not None:
            probe_rows, _ = _fetch(conn, probe)
            result.null_event_time_rows = int(probe_rows[0]["n"]) if probe_rows else 0

    result.column_types = columns
    result.emit_columns = list(result.column_types)
    result.matched_rows = len(rows)

    fired = dedup_module.apply(rows, trigger, rule)
    result.fires = len(fired)
    result.unique_keys = len({dedup_module.dedup_key(row, trigger.key) for row in fired})

    if trigger.event_time:
        counts = Counter(
            row[trigger.event_time].date()
            for row in fired
            if row.get(trigger.event_time) is not None
        )
        result.per_day = dict(counts)

    result.sample = _build_sample(fired, result.emit_columns, sample)
    return result


def _fetch(
    conn: SourceConnection, compiled: CompiledQuery
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Run a compiled query. Returns the rows and each column's semantic type."""
    # Always a mapping: compiled SQL doubles its literal `%`, and binding is
    # what turns them back into one.
    return conn.fetch(compiled.sql, dict(compiled.params))


def _build_sample(
    rows: list[dict[str, Any]], columns: list[str], size: int
) -> list[dict[str, Any]]:
    """Take a sample spread across the window, not just the oldest N rows.

    The first 20 rows of a 120-day backtest all land on day one, which tells
    the reviewer nothing about whether the trigger behaves consistently.
    """
    if size <= 0 or not rows:
        return []
    if len(rows) <= size:
        picked = rows
    else:
        step = len(rows) / size
        picked = [rows[int(i * step)] for i in range(size)]
    return [{c: row.get(c) for c in columns if c in row} for row in picked]
