"""Data sources without a database: schemes, dialects and the edges between them.

What a DSN's scheme selects, what a trigger compiles to in each dialect, and
the rules for which source a trigger reads. The behaviour against live
databases is in test_mysql.py and test_multisource.py.
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta, timezone

import pytest

from rowfire import sources
from rowfire.compile import CompileError, compile_query, describe_sql, quote_ident
from rowfire.definitions import Trigger
from rowfire.definitions import loads as loads_definitions
from rowfire.platform import store

# ------------------------------------------------------------------ schemes


@pytest.mark.parametrize(
    ("dsn", "kind"),
    [
        ("postgresql://u:p@h:5432/db", "postgres"),
        ("postgres://u:p@h/db", "postgres"),
        ("postgresql+psycopg://u:p@h/db", "postgres"),
        ("mysql://u:p@h:3306/db", "mysql"),
        ("mysql+pymysql://u:p@h/db", "mysql"),
        ("mariadb://u:p@h/db", "mysql"),
        ("MySQL://u:p@h/db", "mysql"),
    ],
)
def test_the_scheme_picks_the_engine(dsn: str, kind: str) -> None:
    assert sources.kind_of(dsn) == kind


def test_an_unsupported_scheme_is_refused_without_echoing_the_dsn() -> None:
    with pytest.raises(sources.EngineError) as excinfo:
        sources.kind_of("sqlserver://admin:hunter2@db.internal/prod")
    message = str(excinfo.value)
    assert "sqlserver" in message
    assert "hunter2" not in message
    assert "admin" not in message


def test_a_dsn_with_no_scheme_is_refused() -> None:
    with pytest.raises(sources.EngineError, match="no scheme"):
        sources.kind_of("host=db user=me password=secret")


def test_a_summary_names_the_host_and_database_but_no_credential() -> None:
    summary = sources.summarise("mysql://reader:s3cret@db.example:3306/support?ssl-mode=REQUIRED")
    assert summary == "db.example:3306/support"
    assert "s3cret" not in summary and "reader" not in summary


@pytest.mark.parametrize(
    ("mode", "verified", "hostname"),
    [
        ("REQUIRED", ssl.CERT_NONE, False),
        ("VERIFY_CA", ssl.CERT_REQUIRED, False),
        ("VERIFY_IDENTITY", ssl.CERT_REQUIRED, True),
    ],
)
def test_mysql_tls_follows_the_mysql_clients_ssl_mode(
    mode: str, verified: ssl.VerifyMode, hostname: bool
) -> None:
    context = sources._mysql_ssl({"ssl-mode": mode})
    assert context is not None
    assert context.verify_mode == verified
    assert context.check_hostname is hostname


@pytest.mark.parametrize("mode", ["", "DISABLED", "PREFERRED"])
def test_mysql_tls_is_off_unless_asked_for(mode: str) -> None:
    assert sources._mysql_ssl({"ssl-mode": mode} if mode else {}) is None


def test_a_bound_time_reaches_mysql_as_utc() -> None:
    # PyMySQL drops the zone when it renders a datetime, so an aware value
    # in another zone must be moved to UTC (the session's zone) first.
    cairo = timezone(timedelta(hours=3))
    bound = sources._bind({"since": datetime(2026, 1, 1, 12, 0, tzinfo=cairo)}, "mysql")
    assert bound["since"] == datetime(2026, 1, 1, 9, 0)
    assert bound["since"].tzinfo is None


def test_postgres_keeps_its_aware_datetimes() -> None:
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert sources._bind({"since": moment}, "postgres")["since"] is moment


def test_a_naive_mysql_datetime_is_read_as_utc() -> None:
    row = sources._mysql_row(
        {"at": datetime(2026, 1, 1, 9, 0), "tags": '["sso"]', "n": 1},
        [("at", 12, None, 19, 19, 0, True), ("tags", 245, None, 0, 0, 0, True)],
    )
    assert row["at"] == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    # A JSON column comes back parsed, as it does from Postgres, so a
    # template can reach into it with {{ tags.0 }}.
    assert row["tags"] == ["sso"]


# ----------------------------------------------------------------- dialects


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _trigger(sql: str, event_time: str | None = "created_at") -> Trigger:
    return Trigger(sql=sql, event_time=event_time, key=["id"])


def test_mysql_quotes_with_backticks() -> None:
    assert quote_ident("created_at", "mysql") == "`created_at`"
    assert quote_ident("created_at", "postgres") == '"created_at"'


def test_mysql_syntax_survives_the_round_trip() -> None:
    # Backticks and MySQL-only functions must parse in the MySQL dialect --
    # and would not in Postgres's.
    sql = "SELECT `id`, created_at FROM `tickets` WHERE created_at > NOW() - INTERVAL 3 DAY"
    compiled = compile_query(
        _trigger(sql), since=NOW - timedelta(days=1), until=NOW, dialect="mysql"
    )
    assert "`tickets`" in compiled.sql
    assert "ORDER BY t.`created_at`" in compiled.sql


def test_a_write_is_refused_in_the_mysql_dialect_too() -> None:
    with pytest.raises(CompileError, match="SELECT"):
        compile_query(_trigger("DELETE FROM tickets"), dialect="mysql")


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_a_literal_percent_sign_survives_parameter_binding(dialect: str) -> None:
    # Both drivers format parameters with %(name)s, so a bare `%` in a LIKE
    # pattern would be read as a directive. It is doubled, and the driver
    # halves it again when it binds.
    compiled = compile_query(
        _trigger("SELECT id, created_at FROM t WHERE subject LIKE 'Refund 100%'"),
        since=NOW - timedelta(days=1),
        until=NOW,
        dialect=dialect,
    )
    assert "'Refund 100%%'" in compiled.sql
    assert "%(since)s" in compiled.sql and "%(until)s" in compiled.sql
    # What a person pastes into a client has the single sign back.
    assert "'Refund 100%'" in compiled.display_sql()


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_an_authors_own_window_is_still_bound_next_to_a_percent_sign(dialect: str) -> None:
    compiled = compile_query(
        _trigger(
            "SELECT id, created_at FROM t WHERE subject LIKE '%sso%' "
            "AND created_at >= :since AND created_at < :until"
        ),
        since=NOW - timedelta(days=1),
        until=NOW,
        dialect=dialect,
    )
    assert "'%%sso%%'" in compiled.sql
    assert compiled.sql.count("%(since)s") == 1
    assert set(compiled.params) == {"since", "until"}


def test_a_mysql_describe_casts_the_authors_bounds_to_datetime() -> None:
    described = describe_sql(
        _trigger("SELECT id, created_at FROM t WHERE created_at >= :since"), "mysql"
    )
    assert "CAST(NULL AS DATETIME)" in described.sql
    assert described.sql.endswith("LIMIT 0")


def test_mysql_display_sql_inlines_utc_wall_clock_time() -> None:
    compiled = compile_query(
        _trigger("SELECT id, created_at FROM t"),
        since=datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),
        until=NOW,
        dialect="mysql",
    )
    assert "TIMESTAMP '2026-01-01 10:00:00'" in compiled.display_sql()


# ------------------------------------------------------ which source to read


def test_a_trigger_names_its_source() -> None:
    definitions = loads_definitions(
        """
version: 2
triggers:
  ticket_opened:
    source: support
    sql: SELECT id, created_at FROM tickets
    event_time: created_at
    key: [id]
"""
    )
    assert definitions.triggers["ticket_opened"].source == "support"


def test_a_source_name_is_an_identifier() -> None:
    with pytest.raises(Exception, match="source"):
        Trigger(sql="SELECT 1 AS id", key=["id"], source="Support DB")


def _definitions(source: str | None) -> object:
    line = f"    source: {source}\n" if source else ""
    return loads_definitions(
        "version: 2\ntriggers:\n  t:\n"
        + line
        + "    sql: SELECT id, at FROM x\n    event_time: at\n    key: [id]\n"
    )


def test_naming_no_source_keeps_the_checksum_it_had_before_sources() -> None:
    # Otherwise upgrading would demote every live rule to shadow. This is the
    # checksum exactly as it was computed before a trigger could name one.
    import hashlib
    import json

    material = {"sql": "SELECT id, at FROM x", "event_time": "at", "key": ["id"]}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    before = hashlib.sha256(encoded.encode()).hexdigest()[:32]
    assert store.trigger_checksum(_definitions(None), "t") == before


def test_moving_a_trigger_to_another_source_changes_what_it_means() -> None:
    assert store.trigger_checksum(_definitions("support"), "t") != store.trigger_checksum(
        _definitions(None), "t"
    )
