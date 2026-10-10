"""The hosted demo: one public instance, a private workspace per visitor.

Off unless `ROWFIRE_HOSTED=1`. A local install never touches any of this --
it has one workspace, no sessions, and the loopback guard.

What a visitor gets, on their first visit (`POST /api/session`):

  * a workspace of their own, named only by a signed, HttpOnly cookie
  * their own copy of the sample Postgres tables, in a schema of its own,
    so "Simulate new activity" fires *their* rules and nobody else's
  * the sample data sources connected (the copy, and the shared read-only
    MySQL support desk and Supabase project, when configured) and the sample
    definitions loaded

What keeps a public instance from being a way into anything:

  * data sources are fixed -- nobody can point the server at an address of
    their choosing (see the API's `_require_own_sources`)
  * actions deliver to the Demo inbox only: set `ROWFIRE_EGRESS=inbox-only`
    (dispatch refuses every other URL)
  * state-changing requests must come from this site's own pages (Origin)
  * a cap on live workspaces, and on new sessions per address per hour
  * idle workspaces are deleted, sample schema included (`reap`)

Settings, all read from the environment:

    ROWFIRE_HOSTED=1                    turn this on
    ROWFIRE_HOSTED_DEFINITIONS          the YAML every visitor starts with
    ROWFIRE_DEMO_DSN                    read-only login to the sample Postgres
    ROWFIRE_DEMO_MYSQL_DSN              optional read-only MySQL source
    ROWFIRE_DEMO_SUPABASE_DSN           optional Supabase source, shared read-only:
                                        supabase://<ref>, read with the server's
                                        SUPABASE_ACCESS_TOKEN
                                        (examples/saas/supabase.sql)
    ROWFIRE_DEMO_ACTIVITY_DSN           write login to the sample Postgres, used
                                        to copy the tables and to simulate
    ROWFIRE_WORKSPACE_IDLE_HOURS        default 24
    ROWFIRE_MAX_WORKSPACES              default 200
    ROWFIRE_SESSIONS_PER_ADDRESS_HOUR   default 10
    ROWFIRE_TRUST_PROXY=1               read the client address from
                                        X-Forwarded-For (behind Caddy)
    ROWFIRE_COOKIE_INSECURE=1           drop `Secure` (plain-HTTP testing only)
    ROWFIRE_FEEDBACK_URL                where "Give feedback" goes
    ROWFIRE_HEAD_HTML                   markup added to the page's <head>, e.g.
                                        an analytics tag (any instance, see api)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlmodel import Session, SQLModel, select

from .platform import crypto, store
from .platform.models import Workspace

ENABLED_ENV = "ROWFIRE_HOSTED"
COOKIE = "rowfire_session"

# Visitor schemas are named from the workspace id, never from anything a
# visitor typed, and checked against this before they reach a statement.
_SCHEMA = re.compile(r"^visitor_[0-9a-f]{16}$")

# A visitor's queries are cut off sooner than a local install's: a public
# instance shares its sample database with everyone else on it.
STATEMENT_TIMEOUT_MS = 10_000

# last_seen_at is written at most this often per workspace, so a page that
# polls every few seconds does not turn into a write every few seconds.
_TOUCH_EVERY = timedelta(minutes=5)


# The optional sample sources every visitor shares, by the name a trigger's
# `source:` uses and the setting that holds the connection.
SHARED_SOURCES = (
    ("support", "ROWFIRE_DEMO_MYSQL_DSN"),
    ("supabase", "ROWFIRE_DEMO_SUPABASE_DSN"),
)


class HostedError(Exception):
    """A visitor-facing refusal: the demo is full, or too many sessions."""

    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def idle_hours() -> int:
    return _int_env("ROWFIRE_WORKSPACE_IDLE_HOURS", 24)


def feedback_url() -> str | None:
    return os.environ.get("ROWFIRE_FEEDBACK_URL") or None


def cookie_secure() -> bool:
    return os.environ.get("ROWFIRE_COOKIE_INSECURE", "") not in {"1", "true", "yes"}


# --------------------------------------------------------------- sessions


def _signing_key() -> bytes:
    """Derived from the master key, so there is no second secret to manage."""
    return hmac.new(crypto.load_master_key(), b"rowfire-session-v1", hashlib.sha256).digest()


def sign(workspace_id: uuid.UUID) -> str:
    mac = hmac.new(_signing_key(), workspace_id.bytes, hashlib.sha256).hexdigest()
    return f"{workspace_id.hex}.{mac}"


def verify(token: str | None) -> uuid.UUID | None:
    """The workspace a cookie names, or None if it is not one we signed."""
    if not token or "." not in token:
        return None
    raw, _, mac = token.partition(".")
    try:
        workspace_id = uuid.UUID(hex=raw)
    except ValueError:
        return None
    expected = hmac.new(_signing_key(), workspace_id.bytes, hashlib.sha256).hexdigest()
    return workspace_id if hmac.compare_digest(mac, expected) else None


class _Recent:
    """When this process last wrote each workspace's last_seen_at.

    Only the write is throttled. Whether the workspace still exists is asked
    on every request -- a primary-key lookup -- because the reaper runs in
    the worker, another process: a cached "still there" outlived the delete
    by minutes, and in that time requests ran against a workspace that was
    gone (ensure_workspace even recreated it, as a permanent one).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[uuid.UUID, float] = {}

    def fresh(self, workspace_id: uuid.UUID, within: float) -> bool:
        with self._lock:
            return time.monotonic() - self._seen.get(workspace_id, -1e9) < within

    def mark(self, workspace_id: uuid.UUID) -> None:
        with self._lock:
            self._seen[workspace_id] = time.monotonic()

    def forget(self, workspace_id: uuid.UUID) -> None:
        with self._lock:
            self._seen.pop(workspace_id, None)


_recent = _Recent()


def live(session: Session, workspace_id: uuid.UUID, now: datetime | None = None) -> bool:
    """Whether this visitor's workspace still exists, touching it as it goes."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or not workspace.ephemeral:
        _recent.forget(workspace_id)
        return False
    if not _recent.fresh(workspace_id, _TOUCH_EVERY.total_seconds()):
        workspace.last_seen_at = now or datetime.now(UTC)
        session.add(workspace)
        session.commit()
        _recent.mark(workspace_id)
    return True


class _Throttle:
    """New sessions per client address, in memory. A deterrent, not a wall."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window: float = 3600.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


_sessions_by_address = _Throttle()
# Simulated activity per workspace: generous for clicking, a wall for a script
# that would otherwise grow a visitor's copy of the sample data without end.
_activity_by_workspace = _Throttle()
SIMULATIONS_PER_HOUR = 60


def allow_simulation(workspace_id: uuid.UUID) -> bool:
    return _activity_by_workspace.allow(workspace_id.hex, SIMULATIONS_PER_HOUR)


def client_address(headers: dict[str, str], peer: str | None) -> str:
    """The visitor's address: the proxy's word for it only when told to trust it."""
    if os.environ.get("ROWFIRE_TRUST_PROXY", "") in {"1", "true", "yes"}:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer or "unknown"


# ------------------------------------------------------------ provisioning


def schema_for(workspace_id: uuid.UUID) -> str:
    return f"visitor_{workspace_id.hex[:16]}"


def _with_search_path(dsn: str, schema: str) -> str:
    """The sample DSN, pointed at one visitor's copy of the tables."""
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _admin_dsn() -> str:
    dsn = os.environ.get("ROWFIRE_DEMO_ACTIVITY_DSN", "")
    if not dsn:
        raise HostedError("the hosted demo is missing ROWFIRE_DEMO_ACTIVITY_DSN")
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def _reader_role(dsn: str) -> str:
    user = urlsplit(dsn).username or ""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", user):
        raise HostedError("ROWFIRE_DEMO_DSN must name a plain read-only role")
    return user


_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def template_schema() -> str:
    """Where the sample tables every visitor copies live: `public` by default.

    A hosted deploy that keeps everything in one database (Render's, say)
    puts them in a schema of their own instead, beside the control plane's.
    """
    name = os.environ.get("ROWFIRE_DEMO_TEMPLATE_SCHEMA", "public")
    if not _NAME.match(name):
        raise HostedError(f"ROWFIRE_DEMO_TEMPLATE_SCHEMA is not a plain name: {name!r}")
    return name


def copy_sample(schema: str, reader: str) -> None:
    """Copy every table in the template schema into `schema`.

    Structure and rows, not foreign keys: the copy is only ever read by the
    visitor's triggers and written by the simulate script, and neither needs
    them. Each copied id column gets a sequence of its own in `schema`,
    started past the copied rows, so the copy depends on nothing in the
    template -- which can then be dropped and reseeded on a deploy without
    breaking anyone's workspace.
    """
    import psycopg

    if not _SCHEMA.match(schema):
        raise HostedError(f"refusing an unexpected schema name {schema!r}")
    source = template_schema()
    with psycopg.connect(_admin_dsn(), connect_timeout=5) as conn:
        conn.execute("SET statement_timeout = 30000")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
                (source,),
            ).fetchall()
            if _NAME.match(row[0])
        ]
        conn.execute(f'CREATE SCHEMA "{schema}"')
        for table in tables:
            conn.execute(
                f'CREATE TABLE "{schema}"."{table}" '
                f'(LIKE "{source}"."{table}" INCLUDING DEFAULTS INCLUDING INDEXES)'
            )
            conn.execute(f'INSERT INTO "{schema}"."{table}" SELECT * FROM "{source}"."{table}"')
            serials = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "AND column_default LIKE 'nextval(%%'",
                (schema, table),
            ).fetchall()
            for (column,) in serials:
                if not _NAME.match(column):
                    continue
                sequence = f"{table}_{column}_seq"
                conn.execute(
                    f'CREATE SEQUENCE "{schema}"."{sequence}" '
                    f'OWNED BY "{schema}"."{table}"."{column}"'
                )
                conn.execute(
                    f'SELECT setval(\'"{schema}"."{sequence}"\', '
                    f'COALESCE((SELECT max("{column}") FROM "{schema}"."{table}"), 0) + 1, false)'
                )
                conn.execute(
                    f'ALTER TABLE "{schema}"."{table}" ALTER COLUMN "{column}" '
                    f'SET DEFAULT nextval(\'"{schema}"."{sequence}"\')'
                )
        conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{reader}"')


def drop_sample(schema: str) -> None:
    import psycopg

    if not _SCHEMA.match(schema):
        return
    with psycopg.connect(_admin_dsn(), connect_timeout=5) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def provision(session: Session, *, address: str, now: datetime | None = None) -> Workspace:
    """A new visitor's workspace, ready to use."""
    now = now or datetime.now(UTC)

    live_count = len(
        session.exec(
            select(Workspace).where(
                Workspace.ephemeral,  # type: ignore[arg-type]
                Workspace.last_seen_at > now - timedelta(hours=idle_hours()),  # type: ignore[operator]
            )
        ).all()
    )
    if live_count >= _int_env("ROWFIRE_MAX_WORKSPACES", 200):
        raise HostedError("The demo is full right now. Please try again in a little while.")
    if not _sessions_by_address.allow(address, _int_env("ROWFIRE_SESSIONS_PER_ADDRESS_HOUR", 10)):
        raise HostedError("Too many new demo sessions from here. Please try again later.", 429)

    sample = os.environ.get("ROWFIRE_DEMO_DSN", "")
    definitions_path = os.environ.get("ROWFIRE_HOSTED_DEFINITIONS", "")
    if not sample or not definitions_path:
        raise HostedError("the hosted demo needs ROWFIRE_DEMO_DSN and ROWFIRE_HOSTED_DEFINITIONS")

    reader = _reader_role(sample)
    # The row first, committed, then the copy: the reaper drops any visitor
    # schema without a workspace, so a schema must never exist before its row.
    workspace = Workspace(id=uuid.uuid4(), name="visitor", ephemeral=True, last_seen_at=now)
    session.add(workspace)
    session.commit()
    schema = schema_for(workspace.id)
    try:
        copy_sample(schema, reader)
        store.save_connection(
            session,
            _with_search_path(sample, schema),
            name="primary",
            statement_timeout_ms=STATEMENT_TIMEOUT_MS,
            workspace_id=workspace.id,
        )
        # Shared by every visitor and read-only by construction: MySQL through
        # a SELECT-only login, Supabase through its read-only endpoint.
        connected = {"primary"}
        for name, env in SHARED_SOURCES:
            dsn = os.environ.get(env, "").strip()
            if dsn and readable(dsn):
                store.save_connection(
                    session,
                    dsn,
                    name=name,
                    statement_timeout_ms=STATEMENT_TIMEOUT_MS,
                    workspace_id=workspace.id,
                )
                connected.add(name)
        store.save_definitions(
            session,
            _readable_only(Path(definitions_path).read_text(), connected),
            created_by="hosted",
            workspace_id=workspace.id,
        )
        session.commit()
    except Exception:
        session.rollback()
        delete_workspace(session, workspace.id)
        raise
    _recent.mark(workspace.id)
    return workspace


def readable(dsn: str) -> bool:
    """Whether a shared source can be read here, so it is worth connecting.

    A supabase:// source is read with the server's token: without one, every
    visitor would get a source that fails on every poll.
    """
    if dsn.split("://", 1)[0].lower() != "supabase":
        return True
    from . import supabase

    return bool(os.environ.get(supabase.ACCESS_TOKEN_ENV, "").strip())


def _readable_only(yaml_text: str, sources: set[str]) -> str:
    """The sample definitions without triggers on a source this demo lacks.

    A deploy without the MySQL support desk would otherwise hand every
    visitor a trigger that fails on every poll, and a rule on it that never
    fires. Triggers with no `source` read the default, which always exists.
    """
    import yaml

    raw = yaml.safe_load(yaml_text) or {}
    triggers = raw.get("triggers") or {}
    gone = {
        name for name, t in triggers.items() if (t or {}).get("source", "primary") not in sources
    }
    if not gone:
        return yaml_text
    raw["triggers"] = {name: t for name, t in triggers.items() if name not in gone}
    raw["rules"] = {
        name: r
        for name, r in (raw.get("rules") or {}).items()
        if (r or {}).get("trigger") not in gone
    }
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


# ------------------------------------------------------------------- reap


def _scoped_tables() -> list[Any]:
    """Every table carrying workspace_id, children before parents."""
    return [
        table
        for table in reversed(SQLModel.metadata.sorted_tables)
        if "workspace_id" in table.columns and table.name != "workspace"
    ]


def delete_workspace(session: Session, workspace_id: uuid.UUID) -> None:
    """Remove a workspace and everything in it, then its sample tables.

    Generic over the schema on purpose: a table added later with a
    workspace_id is covered without anyone remembering to list it here.
    """
    for table in _scoped_tables():
        session.execute(table.delete().where(table.c.workspace_id == workspace_id))
    workspace = session.get(Workspace, workspace_id)
    if workspace is not None:
        session.delete(workspace)
    session.commit()
    _recent.forget(workspace_id)
    drop_sample(schema_for(workspace_id))


# One reaper at a time, across however many workers there are.
_REAP_LOCK = 0x726F77666972  # "rowfir"


def reap(session: Session, now: datetime | None = None) -> int:
    """Delete visitor workspaces idle for longer than allowed. Returns how many."""
    now = now or datetime.now(UTC)
    # Session-level, not transaction-level: each workspace is deleted in a
    # transaction of its own, and the lock has to outlive all of them.
    if not session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _REAP_LOCK}).scalar():
        return 0
    try:
        return _reap_locked(session, now)
    finally:
        session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _REAP_LOCK})
        session.commit()


def _reap_locked(session: Session, now: datetime) -> int:
    cutoff = now - timedelta(hours=idle_hours())
    expired = [
        workspace.id
        for workspace in session.exec(
            select(Workspace).where(
                Workspace.ephemeral,  # type: ignore[arg-type]
                Workspace.last_seen_at < cutoff,  # type: ignore[operator]
            )
        ).all()
    ]
    for workspace_id in expired:
        delete_workspace(session, workspace_id)
    _sweep_orphans(session)
    return len(expired)


def _sweep_orphans(session: Session) -> None:
    """Drop visitor schemas whose workspace is gone (a provisioning that died)."""
    import psycopg

    keep = {
        schema_for(w.id)
        for w in session.exec(select(Workspace).where(Workspace.ephemeral)).all()  # type: ignore[arg-type]
    }
    with psycopg.connect(_admin_dsn(), connect_timeout=5) as conn:
        found = [
            row[0]
            for row in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'visitor\\_%'"
            ).fetchall()
        ]
    for schema in found:
        if schema not in keep:
            drop_sample(schema)
