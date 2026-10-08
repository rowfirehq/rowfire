"""Compilation and SQL validation.

The boundary here moved deliberately. A trigger used to be a boolean predicate
over one modelled table, and the validator refused subqueries, joins and any
function not on an allowlist. A trigger is now a whole query, so that boundary
is gone by construction. What is left is narrow and worth testing precisely:
one statement, and that statement reads.

Each rejection test asserts the *reason*, not just that something was refused.
An earlier validator rejected pg_sleep for the wrong reason -- it tripped on
the `AND` connector before ever reaching pg_sleep -- which read as a pass
while leaving the real check unexercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rowfire.compile import (
    CompileError,
    compile_query,
    compile_trigger,
    describe_sql,
    quote_ident,
    uses_own_window,
    validate_sql,
)
from rowfire.definitions import Trigger


def _reject(sql: str) -> list[str]:
    with pytest.raises(CompileError) as excinfo:
        validate_sql(sql)
    return excinfo.value.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE status = 4",
        "SELECT o.id, c.city FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT id FROM orders WHERE status IN (4, 7) AND is_test = false",
        # A subquery is a feature now, not a threat: this is how someone writes
        # "the customer's first order" without a separate enrichment step.
        "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers)",
        "WITH recent AS (SELECT * FROM orders) SELECT id FROM recent",
        "SELECT id FROM orders UNION SELECT id FROM archived_orders",
        "SELECT count(*) OVER () AS n, id FROM orders",
        # No function allowlist any more. Whoever writes this has a role they
        # granted themselves against their own database.
        "SELECT id, lower(status::text) FROM orders WHERE completed_at > now()",
    ],
)
def test_accepts_real_queries(sql: str) -> None:
    assert validate_sql(sql) is not None


def test_rejects_statement_injection() -> None:
    # The `;` case: two statements come back instead of one.
    errors = _reject("SELECT id FROM orders; DROP TABLE orders")
    assert any("exactly one statement" in e for e in errors)


def test_rejects_drop_before_any_query_is_issued() -> None:
    # No connection exists in this test at all, which is the point: a trigger
    # containing DROP never reaches the database.
    errors = _reject("DROP TABLE orders")
    assert any("DROP" in e for e in errors)


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("INSERT INTO orders (id) VALUES (1)", "INSERT"),
        ("UPDATE orders SET status = 4", "UPDATE"),
        ("DELETE FROM orders", "DELETE"),
        ("CREATE TABLE t (id int)", "CREATE"),
        ("ALTER TABLE orders ADD COLUMN x int", "ALTER"),
        ("GRANT SELECT ON orders TO public", "GRANT"),
        ("SET statement_timeout = 0", "SET"),
    ],
)
def test_rejects_writes_by_name(sql: str, kind: str) -> None:
    errors = _reject(sql)
    assert any(kind in e for e in errors), f"expected the error to name {kind}; got {errors}"


def test_rejects_a_write_hidden_in_a_cte() -> None:
    # Postgres really does allow this, and a read-only session would refuse it
    # -- but it is refused here first, so it never leaves the process.
    errors = _reject("WITH x AS (DELETE FROM orders RETURNING id) SELECT id FROM x")
    assert any("DELETE" in e for e in errors)


def test_rejects_empty_sql() -> None:
    assert any("empty" in e for e in _reject("   "))


def test_rejects_unparseable_sql() -> None:
    assert any("parseable" in e for e in _reject("SELECT FROM WHERE ORDER ("))


def test_unsafe_identifier_is_refused() -> None:
    # Output column names come from the database, but they are still checked
    # before being interpolated into a string.
    with pytest.raises(CompileError, match="unsafe identifier"):
        quote_ident('completed_at" FROM pg_authid --')


def test_compiled_sql_is_parameterised(definitions) -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=90)
    compiled = compile_trigger(definitions, "completed_orders", since=since, until=until)

    assert "%(since)s" in compiled.sql
    assert "%(until)s" in compiled.sql
    assert compiled.params["since"] == since
    assert compiled.sql.startswith("SELECT * FROM (")


def test_the_window_wraps_the_query_rather_than_editing_it(completed_orders) -> None:
    # The watermark, the lookback overlap and the ledger all depend on us
    # owning those bounds. A query trusted to filter its own time range would
    # take that away.
    until = datetime.now(UTC)
    compiled = compile_query(completed_orders, since=until - timedelta(days=1), until=until)
    assert 'WHERE t."completed_at" >= %(since)s' in compiled.sql
    assert 'AND t."completed_at" < %(until)s' in compiled.sql
    assert 'ORDER BY t."completed_at"' in compiled.sql


def test_a_query_may_bind_the_window_itself() -> None:
    # The escape hatch: pushing the bound inside the query is sometimes the
    # difference between an index scan and a sequential one.
    trigger = Trigger(
        sql="SELECT id, completed_at FROM orders WHERE completed_at >= :since "
        "AND completed_at < :until",
        event_time="completed_at",
        key=["id"],
    )
    assert uses_own_window(trigger.sql)

    until = datetime.now(UTC)
    compiled = compile_query(trigger, since=until - timedelta(days=1), until=until)
    assert "%(since)s" in compiled.sql
    assert compiled.params["until"] == until
    # No outer predicate: the bounds are where the author put them.
    assert "WHERE t." not in compiled.sql


def test_display_sql_inlines_literals(definitions) -> None:
    until = datetime.now(UTC)
    compiled = compile_trigger(
        definitions, "completed_orders", since=until - timedelta(days=1), until=until
    )
    rendered = compiled.display_sql()
    assert "%(since)s" not in rendered
    assert "TIMESTAMPTZ '" in rendered


def test_row_cap_is_applied(completed_orders) -> None:
    compiled = compile_query(completed_orders, limit=20)
    assert compiled.sql.rstrip().endswith("LIMIT 20")


def test_definitions_row_cap_is_the_default(definitions) -> None:
    compiled = compile_trigger(definitions, "completed_orders")
    assert compiled.sql.rstrip().endswith(f"LIMIT {definitions.source.max_rows}")


def test_unknown_trigger_names_the_known_ones(definitions) -> None:
    with pytest.raises(CompileError) as excinfo:
        compile_trigger(definitions, "nope")
    assert "completed_orders" in excinfo.value.errors[0]


def test_trigger_without_event_time_compiles_timelessly(completed_orders) -> None:
    completed_orders.event_time = None
    compiled = compile_query(
        completed_orders,
        since=datetime.now(UTC) - timedelta(days=1),
    )
    assert compiled.timeless
    assert "%(since)s" not in compiled.sql
    assert "ORDER BY" not in compiled.sql


def test_describe_sql_returns_no_rows(completed_orders) -> None:
    # How the composer learns a query's output columns: cheap, and exact,
    # because the answer comes from the cursor rather than from parsing.
    compiled = describe_sql(completed_orders)
    assert compiled.sql.rstrip().endswith("LIMIT 0")
    assert compiled.params == {}


def test_describe_sql_neutralises_the_authors_placeholders() -> None:
    # Otherwise a query using the escape hatch could not be described at all:
    # psycopg would demand values for :since and :until.
    trigger = Trigger(
        sql="SELECT id, completed_at FROM orders WHERE completed_at >= :since",
        event_time="completed_at",
        key=["id"],
    )
    compiled = describe_sql(trigger)
    assert ":since" not in compiled.sql
    assert "NULL::timestamptz" in compiled.sql


def test_what_runs_is_what_was_checked(completed_orders) -> None:
    # The query is re-rendered from the validated tree, not spliced in as
    # text, so there is no gap between the string that was checked and the
    # string that executes.
    completed_orders.sql = "select ID from ORDERS where STATUS = 4"
    completed_orders.event_time = None
    compiled = compile_query(completed_orders)
    assert "select" not in compiled.sql
