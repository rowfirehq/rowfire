"""The databases Rowfire reads from, and how each is opened read-only.

A data source is a named DSN. Its scheme picks the driver:

    postgresql:// postgres://        Postgres, through psycopg
    mysql:// mysql+pymysql://        MySQL (and MariaDB), through PyMySQL
    mariadb://

Everything above this module -- the engine, the scheduler, the schema reader,
the API -- talks to a `SourceConnection` and does not care which one it got.
What differs is kept here: how a session is made read-only, how a statement
timeout is set, which SQL dialect the query checker parses, and how a value
comes back.

Read-only is enforced at three independent layers on both engines:

  1. the role itself should only have SELECT (the customer's own grant)
  2. the session is set read-only before any query runs
  3. the query checker refuses anything but a single SELECT

Times are UTC end to end. Postgres returns timestamptz as aware datetimes. A
MySQL session is pinned to UTC and its naive DATETIME values are read as UTC,
because a DATETIME column carries no zone of its own; a table that stores
local wall-clock time in one would place its rows hours off.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

Kind = Literal["postgres", "mysql"]

KINDS: tuple[Kind, ...] = ("postgres", "mysql")

_SCHEMES: dict[str, Kind] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "postgresql+psycopg": "postgres",
    "mysql": "mysql",
    "mysql+pymysql": "mysql",
    "mariadb": "mysql",
}

# The sqlglot dialect each engine's SQL is parsed and re-rendered in.
DIALECTS: dict[Kind, str] = {"postgres": "postgres", "mysql": "mysql"}

LABELS: dict[Kind, str] = {"postgres": "PostgreSQL", "mysql": "MySQL"}


class EngineError(Exception):
    """Raised for connection and execution problems, never carrying the DSN."""


class QueryTimeout(EngineError):
    """The statement ran past the session's timeout and was cancelled."""


def kind_of(dsn: str) -> Kind:
    """Which engine a DSN points at, from its scheme alone."""
    scheme = dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""
    kind = _SCHEMES.get(scheme)
    if kind is None:
        # The scheme is safe to echo; nothing after it is.
        shown = scheme or "no scheme"
        raise EngineError(
            f"unsupported database ({shown}). Use a postgresql:// or mysql:// connection string."
        )
    return kind


def summarise(dsn: str) -> str:
    """A human label for a connection that leaks neither password nor user.

    Deliberately hand-rolled rather than str(parsed): urlsplit keeps the
    password in the netloc, and one careless f-string would put it in a log.
    """
    remainder = dsn.split("://", 1)[-1]
    if "@" in remainder:
        remainder = remainder.rsplit("@", 1)[1]
    host_part, _, db_part = remainder.partition("/")
    database = db_part.split("?")[0] or "?"
    return f"{host_part or '?'}/{database}"


def clean(exc: BaseException) -> str:
    """The first line of a driver error, which is where its message lives."""
    return str(exc).strip().split("\n")[0][:300]


class SourceConnection:
    """One open, read-only connection to a customer database.

    `cursor()` and `rollback()` pass straight through, so code that reads a
    catalogue can still talk to the driver directly. Rows always come back as
    dicts keyed by column name.
    """

    def __init__(self, raw: Any, kind: Kind) -> None:
        self.raw = raw
        self.kind: Kind = kind
        self.dialect = DIALECTS[kind]

    def cursor(self) -> Any:
        return self.raw.cursor()

    def rollback(self) -> None:
        self.raw.rollback()

    def fetch(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Run one query. Returns the rows and each column's semantic type."""
        from .introspect import semantic_from_cursor

        # A mapping -- even an empty one -- means the SQL is in bind form, with
        # literal `%` doubled; None means it is plain SQL with nothing to undo.
        bound = _bind(params, self.kind) if params is not None else None
        try:
            with self.raw.cursor() as cur:
                cur.execute(sql, bound)
                rows = list(cur.fetchall())
                description = cur.description
            self.raw.rollback()  # read-only, but leave no transaction open
        except Exception as exc:
            self._rollback_quietly()
            raise _translate(exc, self.kind) from exc

        columns = semantic_from_cursor(description, self.kind)
        if self.kind == "mysql":
            rows = [_mysql_row(row, description) for row in rows]
        return rows, columns

    def describe(self, sql: str) -> dict[str, str]:
        """The columns a zero-row query returns, with their semantic types."""
        _, columns = self.fetch(sql, {})
        return columns

    def server_info(self) -> dict[str, Any]:
        """Version, database name and whether the session is read-only."""
        if self.kind == "postgres":
            rows, _ = self.fetch(
                "SELECT version() AS version, current_database() AS database, "
                "current_setting('default_transaction_read_only') AS read_only"
            )
            info = rows[0] if rows else {}
            return {
                "version": str(info.get("version", "")).split(" on ")[0],
                "database": str(info.get("database", "")),
                "read_only": str(info.get("read_only")) == "on",
            }
        rows, _ = self.fetch(
            "SELECT version() AS version, database() AS `database`, "
            "@@session.transaction_read_only AS read_only"
        )
        info = rows[0] if rows else {}
        return {
            "version": f"MySQL {info.get('version', '')}".strip(),
            "database": str(info.get("database") or ""),
            "read_only": str(info.get("read_only")) == "1",
        }

    def default_schema(self) -> str:
        """Where tables live when nobody says: `public`, or the MySQL database."""
        if self.kind == "postgres":
            # Asked rather than assumed: `public` almost always, but a
            # connection can name its own search_path (a hosted demo gives
            # each visitor a schema of their own that way).
            rows, _ = self.fetch("SELECT current_schema() AS schema")
            return str(rows[0]["schema"] or "public") if rows else "public"
        rows, _ = self.fetch("SELECT database() AS db")
        return str(rows[0]["db"] or "") if rows else ""

    def _rollback_quietly(self) -> None:
        try:
            self.raw.rollback()
        except Exception:  # noqa: BLE001 -- the original error is the one to report
            pass


@contextmanager
def connect(dsn: str, statement_timeout_ms: int = 30_000) -> Iterator[SourceConnection]:
    """Open a hardened read-only connection to whichever engine the DSN names."""
    kind = kind_of(dsn)
    opener = _open_postgres if kind == "postgres" else _open_mysql
    raw = opener(dsn, statement_timeout_ms)
    try:
        yield SourceConnection(raw, kind)
    finally:
        try:
            raw.close()
        except Exception:  # noqa: BLE001 -- closing a dead socket is not news
            pass


# ------------------------------------------------------------- postgres


def _open_postgres(dsn: str, statement_timeout_ms: int) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    # psycopg does not understand the SQLAlchemy-style driver suffix.
    if dsn.lower().startswith("postgresql+psycopg://"):
        dsn = "postgresql://" + dsn.split("://", 1)[1]
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
    except psycopg.Error as exc:
        # Deliberately does not include the DSN in the message.
        raise EngineError(f"could not connect to Postgres: {clean(exc)}") from exc

    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            # SET does not accept bind parameters, so the timeout goes through
            # set_config() rather than being interpolated into the statement.
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(int(statement_timeout_ms)),),
            )
            cur.execute("SELECT set_config('idle_in_transaction_session_timeout', '60000', false)")
        conn.autocommit = False
        conn.read_only = True
    except psycopg.Error as exc:
        conn.close()
        raise EngineError(f"could not prepare the Postgres session: {clean(exc)}") from exc
    return conn


# ---------------------------------------------------------------- mysql

# Errors that mean "the statement timeout fired": MySQL's max_execution_time
# and MariaDB's max_statement_time.
_MYSQL_TIMEOUT_CODES = {3024, 1969}


def _open_mysql(dsn: str, statement_timeout_ms: int) -> Any:
    import pymysql
    from pymysql.cursors import DictCursor

    parts = urlsplit(dsn)
    query = {k.lower(): v[-1] for k, v in parse_qs(parts.query).items()}
    try:
        conn = pymysql.connect(
            host=parts.hostname or "localhost",
            port=parts.port or 3306,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=unquote(parts.path.lstrip("/")) or None,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
            ssl=_mysql_ssl(query),
        )
    except pymysql.MySQLError as exc:
        raise EngineError(f"could not connect to MySQL: {clean(exc)}") from exc

    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET time_zone = '+00:00'")
            try:
                cur.execute("SET SESSION max_execution_time = %s", (int(statement_timeout_ms),))
            except pymysql.MySQLError:
                # MariaDB spells it differently, in seconds.
                cur.execute(
                    "SET SESSION max_statement_time = %s", (max(statement_timeout_ms, 1) / 1000,)
                )
        conn.commit()
    except pymysql.MySQLError as exc:
        conn.close()
        raise EngineError(f"could not prepare the MySQL session: {clean(exc)}") from exc
    return conn


def _mysql_ssl(query: dict[str, str]) -> ssl.SSLContext | None:
    """TLS from the DSN's `ssl-mode`, spelled the way MySQL's own clients do."""
    mode = (query.get("ssl-mode") or query.get("sslmode") or query.get("ssl_mode") or "").lower()
    if mode in ("", "disabled", "preferred"):
        return None
    context = ssl.create_default_context(cafile=query.get("ssl-ca") or None)
    if mode == "required":
        # Encrypted, but the server's certificate is not checked -- the same
        # meaning REQUIRED has in the mysql client.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify_ca":
        context.check_hostname = False
    return context


def _mysql_row(row: dict[str, Any], description: Any) -> dict[str, Any]:
    """Make a MySQL row look like a Postgres one where it matters."""
    json_columns = {col[0] for col in description or () if col[1] == 245}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime) and value.tzinfo is None:
            # The session is pinned to UTC, so a naive value is a UTC one.
            value = value.replace(tzinfo=UTC)
        elif key in json_columns and isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


# --------------------------------------------------------------- shared


def _bind(params: dict[str, Any], kind: Kind) -> dict[str, Any]:
    """Bind parameters the way each driver expects them.

    PyMySQL renders a datetime without its zone, so an aware value is moved
    to UTC first -- the session's zone -- rather than sent as wall-clock time
    in whatever zone it happened to carry.
    """
    if kind != "mysql":
        return params
    out: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        out[key] = value
    return out


def _translate(exc: Exception, kind: Kind) -> EngineError:
    """A driver error as an EngineError, with a timeout called out as one."""
    if kind == "postgres":
        import psycopg

        if isinstance(exc, psycopg.errors.QueryCanceled):
            return QueryTimeout(_TIMEOUT_MESSAGE)
        if isinstance(exc, psycopg.Error):
            return EngineError(f"query failed: {clean(exc)}")
    else:
        import pymysql

        if isinstance(exc, pymysql.MySQLError):
            code = exc.args[0] if exc.args else None
            if code in _MYSQL_TIMEOUT_CODES:
                return QueryTimeout(_TIMEOUT_MESSAGE)
            message = exc.args[1] if len(exc.args) > 1 else clean(exc)
            return EngineError(f"query failed: {clean(Exception(message))}")
    return EngineError(f"query failed: {clean(exc)}")


_TIMEOUT_MESSAGE = (
    "query exceeded statement_timeout. Narrow the window with --days, "
    "or raise source.statement_timeout_ms in the definitions file."
)
