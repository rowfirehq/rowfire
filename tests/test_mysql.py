"""MySQL as a data source, against the support-desk fixture.

Needs `docker compose up -d mysql`; skips cleanly without it. Expected counts
are derived from fixtures/mysql/02_seed.sql, which is deterministic, by
replaying its arithmetic here rather than by pasting numbers in.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from rowfire import engine, introspect, sources
from rowfire.definitions import loads as loads_definitions

MYSQL_DSN = os.environ.get(
    "ROWFIRE_MYSQL_FIXTURE_DSN", "mysql://rowfire_ro:rowfire_ro@127.0.0.1:3307/rowfire_support"
)
# A user that *can* write, to prove the session refuses on its own.
MYSQL_ADMIN_DSN = os.environ.get(
    "ROWFIRE_MYSQL_ADMIN_DSN", "mysql://root:rowfire@127.0.0.1:3307/rowfire_support"
)


def _reachable(dsn: str) -> bool:
    try:
        with sources.connect(dsn) as conn:
            conn.fetch("SELECT 1 AS one")
        return True
    except sources.EngineError:
        return False


pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not _reachable(MYSQL_DSN),
        reason="MySQL fixture unavailable; run `docker compose up -d mysql`",
    ),
]

# Wide enough that every seeded ticket is inside it however long ago the
# fixture was loaded; the seed spans 120 days.
WIDE = 200


# The seed's arithmetic, replayed. n runs 1..480.
SEQ = range(1, 481)


def _spam(n: int) -> bool:
    return n % 20 == 0


def _account(n: int) -> int:
    return 1 + (n * 13) % 60


URGENT = [n for n in SEQ if n % 7 == 0 and not _spam(n)]
UNANSWERED = [n for n in SEQ if n % 9 == 0 and not _spam(n)]
REFUNDS = [n for n in SEQ if n % 11 == 0]


DEFS = """
version: 2
source: { type: mysql, dsn_env: ROWFIRE_MYSQL_FIXTURE_DSN, max_rows: 50000 }
triggers:
  urgent_ticket:
    sql: |
      SELECT t.id, t.account_id, t.subject, t.requester_email, t.created_at,
             a.name AS agent
      FROM tickets t
      LEFT JOIN agents a ON a.id = t.agent_id
      WHERE t.priority = 'urgent' AND t.is_spam = 0
    event_time: created_at
    key: [id]
  urgent_per_account:
    sql: |
      SELECT id, account_id, created_at FROM `tickets`
      WHERE priority = 'urgent' AND is_spam = 0
    event_time: created_at
    key: [account_id]
  refund_request:
    sql: SELECT id, subject, created_at FROM tickets WHERE subject LIKE 'Refund 100%'
    event_time: created_at
    key: [id]
  first_response:
    sql: SELECT id, first_response_at FROM tickets WHERE is_spam = 0
    event_time: first_response_at
    key: [id]
  any_ticket:
    sql: SELECT id, created_at FROM tickets
    event_time: created_at
    key: [id]
  any_ticket_self_bound:
    sql: |
      SELECT id, created_at FROM tickets
      WHERE created_at >= :since AND created_at < :until
    event_time: created_at
    key: [id]
rules:
  page_on_urgent: { trigger: urgent_ticket, policy: once_ever }
  one_page_per_account: { trigger: urgent_per_account, policy: once_ever }
  refunds: { trigger: refund_request, policy: once_ever }
  responses: { trigger: first_response, policy: once_ever }
  every_ticket: { trigger: any_ticket, policy: once_ever }
  every_ticket_self_bound: { trigger: any_ticket_self_bound, policy: once_ever }
"""


@pytest.fixture
def definitions(monkeypatch):
    monkeypatch.setenv("ROWFIRE_MYSQL_FIXTURE_DSN", MYSQL_DSN)
    return loads_definitions(DEFS)


@pytest.fixture
def conn():
    with sources.connect(MYSQL_DSN) as connection:
        yield connection


# ------------------------------------------------------------- read-only


def test_the_session_reports_itself_read_only(conn) -> None:
    info = conn.server_info()
    assert conn.kind == "mysql"
    assert info["database"] == "rowfire_support"
    assert info["read_only"] is True
    assert info["version"].startswith("MySQL")


def test_a_write_is_refused_by_the_grant(conn) -> None:
    with pytest.raises(sources.EngineError, match="denied"):
        conn.fetch("INSERT INTO agents (name, email) VALUES ('x', 'y')", {})


def test_a_write_is_refused_by_the_session_even_for_a_user_who_may_write() -> None:
    if not _reachable(MYSQL_ADMIN_DSN):
        pytest.skip("no MySQL admin account to test the session layer with")
    with sources.connect(MYSQL_ADMIN_DSN) as admin:
        for statement in (
            "INSERT INTO agents (name, email) VALUES ('x', 'y')",
            "CREATE TABLE should_not_exist (id INT)",
        ):
            with pytest.raises(sources.EngineError, match="READ ONLY"):
                admin.fetch(statement, {})


def test_a_connection_error_does_not_include_the_password() -> None:
    with pytest.raises(sources.EngineError) as excinfo:
        with sources.connect("mysql://rowfire_ro:sup3rsecret@127.0.0.1:1/nope"):
            pass
    assert "sup3rsecret" not in str(excinfo.value)


def test_the_statement_timeout_stops_a_runaway_query() -> None:
    with sources.connect(MYSQL_DSN, statement_timeout_ms=1) as slow:
        with pytest.raises(sources.QueryTimeout, match="statement_timeout"):
            slow.fetch(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS a, "
                "information_schema.COLUMNS b, information_schema.COLUMNS c",
                {},
            )


# ---------------------------------------------------------------- schema


def test_the_schema_is_read_from_the_connected_database(conn) -> None:
    tables = introspect.read_schema(conn)
    assert set(tables) == {"agents", "tickets"}

    tickets = tables["tickets"]
    assert tickets.primary_key == "id"
    assert [(fk.column, fk.target_table, fk.target_column) for fk in tickets.foreign_keys] == [
        ("agent_id", "agents", "id")
    ]

    semantic = {c.name: introspect.semantic_type(c).value for c in tickets.columns}
    assert semantic["created_at"] == "timestamp"
    assert semantic["is_spam"] == "boolean"  # tinyint(1)
    assert semantic["tags"] == "json"
    assert semantic["requester_email"] == "email"
    assert semantic["csat"] == "integer"


def test_a_draft_names_mysql_as_its_engine(conn) -> None:
    text = introspect.draft_yaml(introspect.read_schema(conn), kind=conn.kind)
    assert "type: mysql" in text
    loads_definitions(text)


def test_a_bad_column_is_reported_by_validation(conn, definitions) -> None:
    definitions.triggers["any_ticket"].sql = "SELECT id, created_att FROM tickets"
    errors = introspect.validate_definitions(definitions, conn, only=["any_ticket"])
    assert errors and "created_att" in errors[0]


def test_output_columns_are_typed_from_the_cursor(conn, definitions) -> None:
    columns = introspect.describe_trigger(conn, definitions.triggers["urgent_ticket"])
    assert columns["created_at"] == "timestamp"
    assert columns["requester_email"] == "email"
    assert columns["id"] == "identifier"
    assert columns["agent"] == "string"


# -------------------------------------------------------------- backtests


def test_a_backtest_counts_what_the_query_matches(definitions) -> None:
    result = engine.run(definitions, "page_on_urgent", days=WIDE, dsn=MYSQL_DSN)
    assert result.matched_rows == len(URGENT)
    assert result.fires == len(URGENT)
    assert sum(result.per_day.values()) == len(URGENT)
    # A joined column is just a column.
    assert "agent" in result.emit_columns


def test_dedup_applies_to_mysql_rows_the_same_way(definitions) -> None:
    result = engine.run(definitions, "one_page_per_account", days=WIDE, dsn=MYSQL_DSN)
    assert result.matched_rows == len(URGENT)
    assert result.fires == len({_account(n) for n in URGENT})


def test_a_literal_percent_sign_in_the_query_is_not_a_parameter(definitions) -> None:
    result = engine.run(definitions, "refunds", days=WIDE, dsn=MYSQL_DSN)
    assert result.matched_rows == len(REFUNDS)


def test_rows_without_a_clock_are_counted_not_dropped(definitions) -> None:
    result = engine.run(definitions, "responses", days=WIDE, dsn=MYSQL_DSN)
    assert result.null_event_time_rows == len(UNANSWERED)
    assert result.matched_rows == 480 - sum(_spam(n) for n in SEQ) - len(UNANSWERED)


def test_event_times_come_back_as_utc(definitions) -> None:
    result = engine.run(definitions, "every_ticket", days=WIDE, sample=5, dsn=MYSQL_DSN)
    for row in result.sample:
        assert row["created_at"].tzinfo is not None
        assert row["created_at"].utcoffset() == timedelta(0)


def test_the_window_is_placed_in_utc(definitions) -> None:
    # Counted two ways: through the engine, which binds aware datetimes, and
    # by MySQL itself with the bounds written as UTC literals. A zone mix-up
    # anywhere in between shifts the window by hours and the counts disagree.
    now = datetime.now(UTC)
    for days in (1, 3, 10):
        since = now - timedelta(days=days)
        result = engine.run(definitions, "every_ticket", days=days, now=now, dsn=MYSQL_DSN)
        with sources.connect(MYSQL_DSN) as conn:
            rows, _ = conn.fetch(
                "SELECT COUNT(*) AS n FROM tickets "
                "WHERE created_at >= %(a)s AND created_at < %(b)s",
                {"a": since.replace(tzinfo=None), "b": now.replace(tzinfo=None)},
            )
        assert result.matched_rows == rows[0]["n"], days


def test_an_authors_own_window_agrees_with_the_wrapper(definitions) -> None:
    now = datetime.now(UTC)
    wrapped = engine.run(definitions, "every_ticket", days=30, now=now, dsn=MYSQL_DSN)
    bound = engine.run(definitions, "every_ticket_self_bound", days=30, now=now, dsn=MYSQL_DSN)
    assert wrapped.matched_rows == bound.matched_rows > 0


def test_per_day_counts_are_by_utc_day(definitions) -> None:
    result = engine.run(definitions, "every_ticket", days=WIDE, dsn=MYSQL_DSN)
    with sources.connect(MYSQL_DSN) as conn:
        rows, _ = conn.fetch("SELECT DATE(created_at) AS d FROM tickets")
    expected = Counter(row["d"] for row in rows)
    assert result.per_day == dict(expected)


def test_the_row_cap_applies(definitions) -> None:
    definitions.source.max_rows = 7
    result = engine.run(definitions, "every_ticket", days=WIDE, dsn=MYSQL_DSN)
    assert result.matched_rows == 7
