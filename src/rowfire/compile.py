"""Trigger -> SQL.

Returns a SQL string plus params, never a live cursor, so a filter layer or a
different runner can wrap this without rewriting the compiler.

On validating a trigger's SQL
-----------------------------
This used to be much tighter. A trigger was a boolean predicate over one
table, and the validator refused subqueries, joins and any function not on an
allowlist, because arbitrary SQL against a production replica is dangerous.

Triggers are now whole queries, which deletes that boundary by construction --
a trigger can join anything the role can see. The reframing that makes it
defensible: that validator was protecting a surface where a non-engineer typed
SQL. A trigger is written by whoever knows the schema, against their own
database, through a role they granted themselves. Arbitrary SELECT is what
they would write in any reporting tool.

What survives, and is still enforced here:

  * exactly one statement -- a `;` cannot smuggle a second one
  * that statement must be a SELECT; no INSERT/UPDATE/DELETE/DDL/COPY/GRANT
  * the query is re-rendered from the validated syntax tree, so what runs is
    what was checked

And outside this module: a read-only session, a statement timeout, and a hard
row cap.

On the time window
------------------
The window is appended by wrapping the query rather than trusting it to filter
itself:

    SELECT * FROM ( <their query> ) AS t
    WHERE t.<event_time> >= :since AND t.<event_time> < :until

The watermark, the deliberate lookback overlap, and the fire ledger all depend
on us controlling those bounds. A query that filtered its own time range would
take that away.

The one escape hatch: a query that mentions `:since` or `:until` itself gets
them bound directly and no wrapper predicate. Pushing the bound inside the
query is sometimes the difference between an index scan and a sequential one,
and the author is better placed than we are to know where it belongs.

On dialects
-----------
A trigger is parsed and re-rendered in its source's dialect -- `postgres` or
`mysql` -- so MySQL backticks and functions survive the round trip, and the
wrapper quotes the clock column the way that engine expects.

Both drivers bind parameters with `%(name)s`, which makes a literal `%` in
the author's query (`LIKE 'a%'`, `DATE_FORMAT(x, '%Y')`) a formatting
directive. Every literal percent sign is therefore doubled before the window
placeholders go in, and the compiled query is always executed with a params
mapping -- even an empty one -- so the doubling is always undone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import sqlglot
from sqlglot import exp

from .definitions import Definitions, Trigger

DIALECT = "postgres"

# Stands in for a window placeholder while literal `%` signs are escaped, so
# the escaping cannot touch the placeholders themselves.
_SENTINEL = "\x00{name}\x00"

# Postgres identifiers we are willing to interpolate. Output column names come
# from the database itself, but this keeps anything structurally strange out of
# the string regardless.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Statement types that have no business in a trigger. A trigger reads.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Grant,
    exp.Copy,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
)

# A trigger's root must be one of these. Anything else is not a query.
_QUERY_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
    exp.Subquery,
)

_WINDOW_PLACEHOLDERS = ("since", "until")


class CompileError(Exception):
    """Raised when a trigger cannot be compiled. Carries every reason found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class CompiledQuery:
    """A compiled trigger: parameterised SQL plus its bound values."""

    sql: str
    params: dict[str, object] = field(default_factory=dict)
    # True when the trigger declares no event_time: the query is unbounded in
    # time, so there is no distribution and it cannot be polled live.
    timeless: bool = False
    dialect: str = DIALECT

    def display_sql(self) -> str:
        """The same query with literals inlined, for `explain` and demos.

        Pasteable into psql or the mysql client as-is -- placeholders would
        not be.
        """
        rendered = self.sql
        for name, value in self.params.items():
            rendered = rendered.replace(f"%({name})s", _sql_literal(value, self.dialect))
        return rendered.replace("%%", "%")


def _sql_literal(value: object, dialect: str = DIALECT) -> str:
    if isinstance(value, datetime):
        if dialect == "mysql":
            # The session runs in UTC and DATETIME carries no zone.
            from datetime import UTC

            moment = value.astimezone(UTC) if value.tzinfo else value
            return f"TIMESTAMP '{moment.replace(tzinfo=None).isoformat(sep=' ')}'"
        return f"TIMESTAMPTZ '{value.isoformat()}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def quote_ident(name: str, dialect: str = DIALECT) -> str:
    """Quote an identifier after checking it is structurally sane."""
    if not _SAFE_IDENTIFIER.match(name):
        raise CompileError(
            [f"unsafe identifier {name!r}: expected letters, digits and underscores only"]
        )
    quote = "`" if dialect == "mysql" else '"'
    return quote + name + quote


def validate_sql(sql: str, dialect: str = DIALECT) -> exp.Expression:
    """Parse and check a trigger's query. Returns the validated tree."""
    if not sql or not sql.strip():
        raise CompileError(["the query is empty"])

    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except Exception as exc:
        raise CompileError([f"not parseable SQL: {exc}"]) from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise CompileError(
            [
                f"a trigger must be exactly one statement, but this parsed as "
                f"{len(statements)} -- a `;` cannot be used to run a second one"
            ]
        )

    tree = statements[0]
    errors: list[str] = []

    if not isinstance(tree, _QUERY_ROOTS):
        errors.append(
            f"a trigger must be a SELECT, but this is "
            f"{type(tree).__name__.upper()}. Triggers read; they never write."
        )

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            errors.append(
                f"a trigger may not contain {type(node).__name__.upper()}. "
                f"The connection is read-only, so this would fail anyway -- "
                f"refused here so it fails before reaching the database."
            )
            break

    if errors:
        raise CompileError(errors)
    return tree


def uses_own_window(sql: str) -> bool:
    """Whether the query binds the window itself via :since / :until."""
    return any(f":{name}" in sql for name in _WINDOW_PLACEHOLDERS)


def _render(tree: exp.Expression, sql: str, dialect: str, window: str | None) -> str:
    """Re-render a validated query, ready to be executed with a params mapping.

    `window` is what the author's own :since / :until become: a bind
    placeholder, a typed NULL for a describe, or None to leave them alone.
    Literal percent signs are doubled so the driver's parameter formatting
    hands them back unchanged.
    """
    inner = tree.sql(dialect=dialect, pretty=True)
    own = window is not None and uses_own_window(sql)
    if own:
        inner = _substitute_window(inner, _SENTINEL)
    inner = inner.replace("%", "%%")
    if own:
        for name in _WINDOW_PLACEHOLDERS:
            inner = inner.replace(_SENTINEL.format(name=name), window.format(name=name))
    return inner


def _substitute_window(sql: str, replacement: str) -> str:
    """Rewrite the author's :since / :until, however sqlglot rendered them.

    sqlglot parses `:since` as a placeholder and the postgres dialect renders
    it back as `%(since)s`, so by the time we see the string the colon form is
    already gone. Both spellings are handled because which one appears is
    sqlglot's choice, not a contract -- and a replacement that silently matches
    nothing is exactly how describe_sql came to emit an unbound parameter.
    """
    for name in _WINDOW_PLACEHOLDERS:
        sql = sql.replace(f":{name}", replacement.format(name=name))
        sql = sql.replace(f"%({name})s", replacement.format(name=name))
    return sql


def compile_trigger(
    definitions: Definitions,
    trigger_name: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    dialect: str = DIALECT,
) -> CompiledQuery:
    """Compile a named trigger into one read-only query."""
    trigger = definitions.triggers.get(trigger_name)
    if trigger is None:
        known = ", ".join(sorted(definitions.triggers)) or "none defined"
        raise CompileError([f"unknown trigger `{trigger_name}`. Known triggers: {known}"])

    return compile_query(
        trigger,
        since=since,
        until=until,
        limit=limit if limit is not None else definitions.source.max_rows,
        dialect=dialect,
    )


def compile_query(
    trigger: Trigger,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    dialect: str = DIALECT,
) -> CompiledQuery:
    """Wrap a trigger's query with the time window, ordering and row cap."""
    tree = validate_sql(trigger.sql, dialect)

    # Re-render from the validated tree rather than reusing the original text,
    # so there is no gap between what was checked and what runs. The author's
    # own :since / :until, if any, become bind placeholders.
    inner = _render(tree, trigger.sql, dialect, "%({name})s")

    params: dict[str, object] = {}
    own_window = uses_own_window(trigger.sql)
    if own_window:
        # The author placed the bounds themselves; honour that and bind.
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until

    indented = "\n".join("    " + line for line in inner.splitlines())
    sql = f"SELECT * FROM (\n{indented}\n) AS t"

    clauses: list[str] = []
    if trigger.event_time and not own_window:
        column = f"t.{quote_ident(trigger.event_time, dialect)}"
        if since is not None:
            clauses.append(f"{column} >= %(since)s")
            params["since"] = since
        if until is not None:
            clauses.append(f"{column} < %(until)s")
            params["until"] = until

    if clauses:
        sql += "\nWHERE " + "\n  AND ".join(clauses)

    if trigger.event_time:
        sql += f"\nORDER BY t.{quote_ident(trigger.event_time, dialect)}"

    if limit is not None:
        sql += f"\nLIMIT {int(limit)}"

    return CompiledQuery(
        sql=sql, params=params, timeless=trigger.event_time is None, dialect=dialect
    )


def compile_null_event_time_probe(trigger: Trigger, dialect: str = DIALECT) -> CompiledQuery | None:
    """Count rows the trigger returns that have no event_time.

    These satisfy the query but cannot be placed on the timeline. Reported
    rather than dropped -- a large number usually means the wrong column was
    named, and the probe carries no time filter because a row with no
    timestamp cannot be placed in a window at all.
    """
    if not trigger.event_time:
        return None

    tree = validate_sql(trigger.sql, dialect)
    if uses_own_window(trigger.sql):
        # The query filters itself by time, so "rows outside any window" is
        # not a question it can answer without the bounds.
        return None

    inner = _render(tree, trigger.sql, dialect, None)
    indented = "\n".join("    " + line for line in inner.splitlines())
    return CompiledQuery(
        sql=(
            f"SELECT count(*) AS n FROM (\n{indented}\n) AS t\n"
            f"WHERE t.{quote_ident(trigger.event_time, dialect)} IS NULL"
        ),
        params={},
        dialect=dialect,
    )


def describe_sql(trigger: Trigger, dialect: str = DIALECT) -> CompiledQuery:
    """A zero-row query whose cursor description gives the output columns."""
    tree = validate_sql(trigger.sql, dialect)
    # A describe takes no parameters, so the author's bounds become typed
    # NULLs. Leaving them bindable would make this query unrunnable -- which
    # is the one thing a describe must never be.
    null = "CAST(NULL AS DATETIME)" if dialect == "mysql" else "NULL::timestamptz"
    inner = _render(tree, trigger.sql, dialect, null)
    indented = "\n".join("    " + line for line in inner.splitlines())
    return CompiledQuery(
        sql=f"SELECT * FROM (\n{indented}\n) AS t\nLIMIT 0", params={}, dialect=dialect
    )
