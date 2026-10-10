"""Local HTTP API behind the demo UI.

This is a single-user tool bound to loopback, not a service. That is a
deliberate product constraint, not a shortcut: the entire risk-removal in a
first design-partner conversation is "a read-only user, nothing leaves your
network, nothing sends". A hosted UI where a prospect pastes a production DSN
into someone else's website reintroduces exactly the objection the backtest
exists to remove.

Consequences, all of which make this smaller rather than larger:

  * no auth, no accounts, no multi-tenancy
  * the DSN is never logged and never returned to the browser
  * binds 127.0.0.1 only, and rejects requests whose Host header is not
    loopback -- otherwise any web page the user visits could drive this server
    against their production replica (DNS rebinding)

Everything the UI reads or writes lives in the control plane: definitions as
immutable versions, the customer connection encrypted at rest. There is no
file mode and no fallback. The server refuses to start without a control
plane (see cli.serve), because a page that loads and then reports every panel
as broken is a worse error message than not starting at all.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text
from sqlmodel import select

from . import demo_activity, engine, hosted, introspect, sources, supabase, tenancy
from .compile import CompileError, compile_trigger
from .definitions import DefinitionError, Definitions
from .definitions import loads as loads_definitions
from .platform import crypto, oauth, store
from .platform import db as platform_db
from .platform.models import AuthKind, Fire, Mode, RuleState, Run, TriggerState

API_PREFIX = "/api"


# ------------------------------------------------------------- control plane


_PLATFORM_READY: bool | None = None
_PLATFORM_CHECKED_AT: float = 0.0
# A negative result is re-probed. Caching "no control plane" forever means a
# control plane that was briefly unreachable at startup -- which compose makes
# likely -- leaves the UI silently degraded for the life of the process.
_NEGATIVE_TTL_SECONDS = 10.0


def platform_ready() -> bool:
    """Whether the control plane is reachable right now.

    Not a mode any more -- the server refuses to start without one. This exists
    so a plane that goes away *while running* is reported as a fault the UI can
    show, rather than as a stack trace in every panel.

    Checked once and cached, because probing the database on every request
    would turn an outage into a slow UI rather than a clear message. A negative
    is re-probed, so recovery needs no restart.
    """
    global _PLATFORM_READY, _PLATFORM_CHECKED_AT

    import os
    import time

    moment = time.monotonic()
    if _PLATFORM_READY is True:
        return True
    if _PLATFORM_READY is False and moment - _PLATFORM_CHECKED_AT < _NEGATIVE_TTL_SECONDS:
        return False

    _PLATFORM_READY = False
    _PLATFORM_CHECKED_AT = moment
    if os.environ.get(platform_db.PLATFORM_DSN_ENV) and os.environ.get(crypto.MASTER_KEY_ENV):
        try:
            from sqlalchemy import text as _text

            with platform_db.session_scope() as session:
                session.execute(_text("SELECT 1 FROM trigger_state LIMIT 1"))
            _PLATFORM_READY = True
        except Exception:  # noqa: BLE001 -- absence is a mode, not an error
            _PLATFORM_READY = False
    return _PLATFORM_READY


def reset_platform_cache() -> None:
    """Forget the probe result. For tests."""
    global _PLATFORM_READY, _PLATFORM_CHECKED_AT
    _PLATFORM_READY = None
    _PLATFORM_CHECKED_AT = 0.0


# --------------------------------------------------------------- session


@dataclass
class Session:
    """Process-lifetime settings. One user, any number of data sources.

    Holds no credential: every DSN lives encrypted in the control plane and is
    decrypted for the request that needs it, so the UI and the worker can
    never disagree about which database a source points at.
    """

    statement_timeout_ms: int = 30_000
    # Set false to demo against definitions the UI must not change.
    allow_write: bool = True


SESSION = Session()


# ---------------------------------------------------------------- models


class ConnectRequest(BaseModel):
    dsn: str = Field(min_length=1)
    # Which data source this is. `primary` is the one a trigger that names
    # no source reads, and what every install stored before there could be
    # more than one.
    name: str = Field(default="primary", pattern=r"^[a-z][a-z0-9_]*$", max_length=63)


class ConnectResponse(BaseModel):
    connected: bool
    name: str = "primary"
    # postgres or mysql, from the DSN's scheme.
    kind: str = "postgres"
    server_version: str
    database: str
    table_count: int
    read_only: bool
    # Echoed so the UI can show *which* database without ever holding the DSN.
    host_summary: str
    # True when the credential was persisted (encrypted) rather than held
    # only in memory. The UI says which, because the difference matters.
    stored: bool = False


class BacktestRequest(BaseModel):
    # A backtest is of a *rule*: the trigger supplies the rows, the rule
    # decides how many of them survive dedup, and that count is the answer.
    rule: str
    days: int = Field(default=90, ge=1, le=3650)
    sample: int = Field(default=20, ge=0, le=500)


class SaveDefinitionsRequest(BaseModel):
    yaml_text: str = Field(min_length=1)


# ------------------------------------------------------------ serialising


def jsonable(value: Any) -> Any:
    """Convert database values into something JSON can carry.

    Decimals become floats only at the edge, never during dedup or counting.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def serialise_result(result: engine.BacktestResult) -> dict[str, Any]:
    days = result.day_range()
    busiest = result.busiest_day()
    quietest = result.quietest_day()

    return {
        "rule": result.rule_name,
        "trigger": result.trigger_name,
        "days": result.days,
        "since": result.since.isoformat() if result.since else None,
        "until": result.until.isoformat() if result.until else None,
        "fires": result.fires,
        "matched_rows": result.matched_rows,
        "dedup_removed": result.dedup_removed,
        "unique_keys": result.unique_keys,
        "null_event_time_rows": result.null_event_time_rows,
        "mean_per_day": round(result.mean_per_day, 2),
        "query_ms": round(result.query_ms, 1),
        "timeless": result.timeless,
        "busiest_day": {"date": busiest[0].isoformat(), "count": busiest[1]} if busiest else None,
        "quietest_day": {"date": quietest[0].isoformat(), "count": quietest[1]}
        if quietest
        else None,
        "series": [{"date": day.isoformat(), "count": result.per_day.get(day, 0)} for day in days],
        "zero_days": sum(1 for day in days if result.per_day.get(day, 0) == 0),
        "columns": result.emit_columns,
        # Read from the cursor, so the query's own output is the authority.
        "column_types": result.column_types,
        "sample": [
            {column: jsonable(row.get(column)) for column in result.emit_columns}
            for row in result.sample
        ],
        "sql": result.compiled.display_sql() if result.compiled else None,
    }


def serialise_tables(tables: dict[str, introspect.Table]) -> list[dict[str, Any]]:
    """The schema as reference material for someone writing SQL."""
    out: list[dict[str, Any]] = []
    for table in sorted(tables.values(), key=lambda t: t.name):
        out.append(
            {
                "name": table.name,
                "primary_key": table.primary_key,
                "timestamps": introspect.timestamp_columns(table),
                "columns": [
                    {
                        "name": column.name,
                        "pg_type": column.pg_type,
                        "semantic_type": introspect.semantic_type(column).value,
                        "nullable": column.nullable,
                    }
                    for column in table.columns
                ],
                "foreign_keys": [
                    {
                        "column": fk.column,
                        "target_table": fk.target_table,
                        "target_column": fk.target_column,
                    }
                    for fk in table.foreign_keys
                ],
            }
        )
    return out


def summarise_dsn(dsn: str) -> str:
    """A human label for a connection that leaks neither password nor user."""
    return sources.summarise(dsn)


# ------------------------------------------------------------------ routes

router = APIRouter(prefix=API_PREFIX)


@router.get("/health")
def health() -> dict[str, Any]:
    import os

    halted: str | None = None
    degraded: str | None = None
    version: int | None = None

    in_session = not (hosted.enabled() and tenancy.current() == NO_WORKSPACE)
    try:
        if not in_session:
            raise _NoSession
        with platform_db.session_scope() as session:
            workspace = store.ensure_workspace(session, tenancy.current())
            halted = workspace.halted_reason if workspace.halted else None
            active = store.active_definitions(session, workspace_id=tenancy.current())
            version = active[0].version if active else None
    except _NoSession:
        pass
    except Exception as exc:  # noqa: BLE001
        # A health endpoint that raises is useless exactly when it is needed.
        # Report the fault instead, so the UI can say what is wrong rather
        # than rendering a confident wrong answer.
        degraded = f"control plane unreachable: {str(exc).splitlines()[0][:160]}"

    return {
        "ok": True,
        "connected": in_session and _safe_has_source(),
        "allow_write": SESSION.allow_write,
        # The public demo: each visitor in a workspace of their own, which
        # the UI makes on first load (POST /api/session) and which is deleted
        # after `idle_hours` without a visit.
        "hosted": hosted.enabled(),
        "session": in_session,
        "idle_hours": hosted.idle_hours() if hosted.enabled() else None,
        "feedback_url": hosted.feedback_url(),
        # The version in force, or null when nothing has been stored yet --
        # which is the UI's cue to offer to generate a starting set.
        "definitions_version": version,
        "halted": halted,
        # Non-null when the control plane is misbehaving. The UI must not make
        # a claim about stored state while this is set.
        "degraded": degraded,
        "suggested_dsn": os.environ.get("ROWFIRE_DEMO_DSN") or None,
        # Whether "Simulate new activity" exists here. Only demo deployments
        # configure it; see demo_activity.
        "demo_activity": demo_activity.configured(),
        # One-click demo sources, per engine, offered when adding a source.
        "suggested_sources": [
            {"name": name, "kind": kind, "dsn": dsn}
            for name, kind, env in (
                ("primary", "postgres", "ROWFIRE_DEMO_DSN"),
                ("support", "mysql", "ROWFIRE_DEMO_MYSQL_DSN"),
                ("supabase", "supabase", "ROWFIRE_DEMO_SUPABASE_DSN"),
            )
            if (dsn := os.environ.get(env)) and not hosted.enabled() and hosted.readable(dsn)
        ],
    }


class _NoSession(Exception):
    """Health asked about a hosted visitor who has no workspace yet."""


# What a hosted request without a session acts on: no workspace at all. Only
# the endpoints open without a session (health, session, catalogue) run with
# it, and none of them reads workspace state under it.
NO_WORKSPACE = uuid.UUID(int=0)


@router.post("/session")
def start_session(request: Request, response: Response) -> dict[str, Any]:
    """Give a hosted demo visitor a workspace of their own.

    Idempotent for someone who already has one. Everything a new visitor
    needs -- their copy of the sample data, the sources, the definitions --
    is made here, so the first page they see is already set up.
    """
    if not hosted.enabled():
        raise HTTPException(status_code=404, detail="Not a hosted demo.")
    _require_platform()
    if tenancy.current() != NO_WORKSPACE:
        return {"workspace": tenancy.current().hex, "created": False}

    address = hosted.client_address(
        {k.lower(): v for k, v in request.headers.items()},
        request.client.host if request.client else None,
    )
    try:
        with platform_db.session_scope() as session:
            workspace = hosted.provision(session, address=address)
            workspace_id = workspace.id
    except hosted.HostedError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    response.set_cookie(
        hosted.COOKIE,
        hosted.sign(workspace_id),
        max_age=hosted.idle_hours() * 3600,
        httponly=True,
        secure=hosted.cookie_secure(),
        samesite="lax",
        path="/",
    )
    return {"workspace": workspace_id.hex, "created": True}


def _require_own_sources() -> None:
    """Refuse to add, replace or remove a data source on the hosted demo.

    There, a source is a connection the server makes on a visitor's behalf,
    and letting a stranger choose its address would make the server reach
    wherever they point it. Every visitor gets the sample sources instead.
    """
    if hosted.enabled():
        raise HTTPException(
            status_code=403,
            detail="Data sources are fixed in the hosted demo. Run Rowfire yourself "
            "to connect your own database.",
        )


@router.post("/connect", response_model=ConnectResponse)
def connect(payload: ConnectRequest) -> ConnectResponse:
    """Validate a DSN and store it as a data source (`primary` unless named).

    Kept for the CLI and for anything written against the single-database
    API. The UI adds sources through POST /api/sources, which is this.
    """
    _require_own_sources()
    return _add_source(payload.name, payload.dsn)


def _add_source(name: str, dsn: str) -> ConnectResponse:
    """Prove a DSN works read-only, then store it encrypted under `name`."""
    dsn = dsn.strip()
    try:
        kind = sources.kind_of(dsn)
        with engine.connect(dsn, SESSION.statement_timeout_ms) as conn:
            info = conn.server_info()
            tables = introspect.read_schema(conn)
    except engine.EngineError as exc:
        # Neither message carries the DSN: kind_of echoes only the scheme,
        # and the driver errors are cut to their first line.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Encrypted before it reaches a column; see platform/crypto.py. Stored
    # unconditionally, because the worker reads this connection too -- a DSN
    # that lived only in this process would mean the UI and the scheduler
    # querying different databases.
    with platform_db.session_scope() as session:
        store.save_connection(
            session,
            dsn,
            name=name,
            statement_timeout_ms=SESSION.statement_timeout_ms,
            workspace_id=tenancy.current(),
        )

    return ConnectResponse(
        connected=True,
        name=name,
        kind=kind,
        server_version=info["version"],
        database=info["database"],
        table_count=len(tables),
        read_only=info["read_only"],
        host_summary=summarise_dsn(dsn),
        stored=True,
    )


def _has_source() -> bool:
    with platform_db.session_scope() as session:
        return bool(store.list_connections(session, workspace_id=tenancy.current()))


def _safe_has_source() -> bool:
    """_has_source, but never raises -- health must survive a broken plane."""
    try:
        return _has_source()
    except Exception:  # noqa: BLE001
        return False


def _source_dsn(name: str | None = None) -> tuple[str, str]:
    """The (name, DSN) of a stored source, or of the default when unnamed.

    Decrypted per request rather than cached, so there is one place a DSN can
    come from.
    """
    try:
        with platform_db.session_scope() as session:
            chosen = name or store.default_source(session, workspace_id=tenancy.current())
            if chosen is None:
                raise HTTPException(
                    status_code=409,
                    detail="No data source yet. Add one under Data sources first.",
                )
            connection = store.get_connection(session, name=chosen, workspace_id=tenancy.current())
            if connection is None:
                raise HTTPException(status_code=404, detail=f"No data source named `{chosen}`.")
            return chosen, store.reveal_dsn(connection)
    except crypto.CryptoError as exc:
        # Wrong master key: say so rather than crashing every request.
        raise HTTPException(
            status_code=409,
            detail="The stored data source cannot be decrypted with this master key.",
        ) from exc


def _require_dsn(source: str | None = None) -> str:
    return _source_dsn(source)[1]


def _trigger_source(definitions: Definitions, trigger_name: str) -> str | None:
    trigger = definitions.triggers.get(trigger_name)
    return trigger.source if trigger is not None else None


@router.post("/disconnect")
def disconnect() -> dict[str, bool]:
    """Nothing is held in memory any more; kept so old clients get an answer."""
    return {"connected": _safe_has_source()}


# ---------------------------------------------------------- data sources


class SourceDraft(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    dsn: str = Field(min_length=1)


@router.get("/sources")
def list_sources() -> dict[str, Any]:
    """Every data source, and which triggers read it. Never a credential."""
    _require_platform()
    definitions = _try_load_current()
    out: list[dict[str, Any]] = []
    with platform_db.session_scope() as session:
        default = store.default_source(session, workspace_id=tenancy.current())
        for connection in store.list_connections(session, workspace_id=tenancy.current()):
            try:
                dsn = store.reveal_dsn(connection)
                kind: str | None = sources.kind_of(dsn)
                summary = summarise_dsn(dsn)
            except (crypto.CryptoError, engine.EngineError):
                kind, summary = None, "cannot be read with this master key"
            readers = (
                sorted(
                    name
                    for name, trigger in definitions.triggers.items()
                    if (trigger.source or default) == connection.name
                )
                if definitions is not None
                else []
            )
            out.append(
                {
                    "name": connection.name,
                    "kind": kind,
                    "label": sources.LABELS.get(kind, "Unknown") if kind else "Unknown",
                    "summary": summary,
                    "default": connection.name == default,
                    "triggers": readers,
                    "created_at": connection.created_at.isoformat(),
                }
            )
    return {"sources": out, "default": default, "kinds": list(sources.KINDS)}


@router.post("/sources", response_model=ConnectResponse)
def add_source(draft: SourceDraft) -> ConnectResponse:
    """Add a data source, or replace the DSN of one with the same name."""
    _require_platform()
    _require_write()
    _require_own_sources()
    return _add_source(draft.name, draft.dsn)


@router.delete("/sources/{name}")
def delete_source(name: str) -> dict[str, Any]:
    """Remove a data source nothing reads.

    Refused while a trigger reads it, explicitly or as the default: deleting
    it would leave that trigger polling nothing, and the error would surface
    on the worker at 3am rather than here.
    """
    _require_platform()
    _require_write()
    _require_own_sources()
    definitions = _try_load_current()
    with platform_db.session_scope() as session:
        default = store.default_source(session, workspace_id=tenancy.current())
        if definitions is not None:
            readers = sorted(
                trigger_name
                for trigger_name, trigger in definitions.triggers.items()
                if (trigger.source or default) == name
            )
            if readers:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"`{name}` is read by {', '.join(readers)}. Point those triggers "
                        f"at another source, or delete them, first."
                    ),
                )
        if not store.delete_connection(session, name, workspace_id=tenancy.current()):
            raise HTTPException(status_code=404, detail=f"No data source named `{name}`.")
    return {"deleted": True, "name": name}


# ------------------------------------------------------------ supabase
#
# Connect a Supabase project without a database password. The customer
# approves Rowfire's OAuth app (database:read and projects:read), picks a
# project, and the source becomes `supabase://<ref>?grant=<id>`: queries go
# through Supabase's read-only query endpoint with tokens refreshed from the
# grant. See supabase.py for the source itself and platform/oauth.py for the
# grant.

SUPABASE_CALLBACK_PATH = "/oauth/supabase/callback"
_SUPABASE_FLOW_COOKIE = "rowfire_supabase_oauth"
_SUPABASE_FLOW_SECONDS = 600
PUBLIC_URL_ENV = "ROWFIRE_PUBLIC_URL"


class SupabaseAuthorize(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")


class SupabaseSourceDraft(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    grant: str = Field(min_length=1, max_length=64)
    project_ref: str = Field(pattern=r"^[a-z]{20}$")


def _supabase_redirect_uri(request: Request) -> str:
    """Where Supabase sends the browser back: this server, as the browser sees it.

    ROWFIRE_PUBLIC_URL wins, for a proxy that rewrites the host. It must match
    a callback URL registered on the OAuth app exactly.
    """
    base = os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}{SUPABASE_CALLBACK_PATH}"


@router.get("/sources/supabase")
def supabase_status() -> dict[str, Any]:
    """Whether "Connect Supabase" can be offered, and how."""
    return {
        "oauth": supabase.oauth_client() is not None and not hosted.enabled(),
        "access_token": bool(os.environ.get(supabase.ACCESS_TOKEN_ENV, "").strip()),
    }


@router.post("/sources/supabase/authorize")
def supabase_authorize(payload: SupabaseAuthorize, request: Request, response: Response):
    """Start the OAuth flow: the URL to send the browser to.

    The state and the PKCE verifier ride in a short-lived cookie, encrypted
    with the master key, rather than in server memory -- so the callback
    works whichever process answers it, and a forged callback without the
    cookie is refused.
    """
    _require_platform()
    _require_write()
    _require_own_sources()
    client = supabase.oauth_client()
    if client is None:
        raise HTTPException(
            status_code=409,
            detail=f"Supabase OAuth is not configured on this server. Set "
            f"{supabase.CLIENT_ID_ENV} and {supabase.CLIENT_SECRET_ENV}.",
        )

    state = secrets.token_urlsafe(24)
    verifier, challenge = supabase.pkce_pair()
    redirect_uri = _supabase_redirect_uri(request)
    flow = {
        "state": state,
        "verifier": verifier,
        "name": payload.name,
        "redirect_uri": redirect_uri,
        "workspace": tenancy.current().hex,
        "started": datetime.now(UTC).isoformat(),
    }
    response.set_cookie(
        _SUPABASE_FLOW_COOKIE,
        oauth.seal_flow(flow),
        max_age=_SUPABASE_FLOW_SECONDS,
        httponly=True,
        secure=redirect_uri.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return {"url": supabase.authorize_url(client, redirect_uri, state, challenge)}


def supabase_callback(request: Request) -> Response:
    """Supabase's redirect back: trade the code for tokens and keep them.

    Mounted outside /api (see create_app) at the exact URL registered on the
    OAuth app. Always answers with a redirect into the UI, which then asks
    which project to read; an error rides along as a query parameter rather
    than as a bare JSON page the person cannot do anything with.
    """
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    def back(**params: str) -> Response:
        redirect = RedirectResponse(f"/sources?{urlencode(params)}", status_code=303)
        redirect.delete_cookie(_SUPABASE_FLOW_COOKIE, path="/")
        return redirect

    query = request.query_params
    if query.get("error"):
        reason = query.get("error_description") or query.get("error") or "access denied"
        return back(supabase_error=f"Supabase did not grant access: {reason[:200]}")

    try:
        flow = oauth.open_flow(request.cookies.get(_SUPABASE_FLOW_COOKIE, ""))
    except oauth.GrantError:
        return back(
            supabase_error="The Supabase sign-in expired or was started elsewhere. Try again."
        )

    # From here on the name typed before leaving is known; an error carries it
    # back so the form does not fall back to `primary`, which would replace it.
    name = str(flow.get("name") or "")
    started = datetime.fromisoformat(flow["started"])
    if datetime.now(UTC) - started > timedelta(seconds=_SUPABASE_FLOW_SECONDS):
        return back(supabase_error="The Supabase sign-in took too long. Try again.", name=name)
    if not secrets.compare_digest(str(query.get("state", "")), flow["state"]):
        return back(
            supabase_error="The Supabase sign-in did not match this browser. Try again.", name=name
        )
    if flow["workspace"] != tenancy.current().hex:
        return back(supabase_error="The Supabase sign-in belongs to another workspace.", name=name)
    code = query.get("code")
    if not code:
        return back(supabase_error="Supabase sent no authorization code.", name=name)

    client = supabase.oauth_client()
    if client is None:
        return back(supabase_error="Supabase OAuth is not configured on this server.", name=name)
    try:
        tokens = supabase.exchange_code(client, code, flow["verifier"], flow["redirect_uri"])
    except supabase.SupabaseError as exc:
        return back(supabase_error=f"Could not finish connecting Supabase: {exc}", name=name)

    with platform_db.session_scope() as session:
        grant = oauth.save_grant(session, tokens, workspace_id=tenancy.current())
        grant_id = grant.id.hex
    return back(supabase_grant=grant_id, name=flow["name"])


def _grant_token(grant_id: str) -> str:
    """An access token for a grant in this workspace, or an HTTP error."""
    with platform_db.session_scope() as session:
        grant = oauth.get_grant(session, grant_id, workspace_id=tenancy.current())
        if grant is None:
            raise HTTPException(status_code=404, detail="That Supabase connection has expired.")
        try:
            return oauth.fresh_tokens(session, grant.id).access_token
        except (oauth.GrantError, crypto.CryptoError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sources/supabase/projects")
def supabase_projects(grant: str) -> dict[str, Any]:
    """The projects a fresh grant can read, for the person to pick one."""
    _require_platform()
    token = _grant_token(grant)
    try:
        projects = supabase.list_projects(token)
    except supabase.SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"projects": projects}


@router.post("/sources/supabase", response_model=ConnectResponse)
def add_supabase_source(draft: SupabaseSourceDraft) -> ConnectResponse:
    """Store a picked project as a source that reads through the grant."""
    _require_platform()
    _require_write()
    _require_own_sources()
    _grant_token(draft.grant)  # the grant exists, is ours, and still works
    grant_id = uuid.UUID(draft.grant).hex
    return _add_source(draft.name, supabase.make_dsn(draft.project_ref, grant_id))


def _try_load_current() -> Definitions | None:
    """The definitions in force, or None when there are none yet."""
    with platform_db.session_scope() as session:
        active = store.active_definitions(session, workspace_id=tenancy.current())
    return active[1] if active is not None else None


@router.get("/schema")
def schema(source: str | None = None, schema_name: str | None = None) -> dict[str, Any]:
    """A source's live schema, as reference for someone writing a trigger.

    `schema_name` defaults to where that source keeps its tables: `public`
    on Postgres, the connected database on MySQL.
    """
    name, dsn = _source_dsn(source)
    try:
        with engine.connect(dsn, SESSION.statement_timeout_ms) as conn:
            chosen = schema_name or conn.default_schema()
            tables = introspect.read_schema(conn, chosen)
            kind = conn.kind
    except engine.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "source": name,
        "kind": kind,
        "schema": chosen,
        "tables": serialise_tables(tables),
        "table_count": len(tables),
    }


@router.post("/definitions/draft")
def draft_definitions(source: str | None = None, schema_name: str | None = None) -> dict[str, Any]:
    """Generate a starter file without writing it, so the UI can preview."""
    _, dsn = _source_dsn(source)
    try:
        with engine.connect(dsn, SESSION.statement_timeout_ms) as conn:
            chosen = schema_name or conn.default_schema()
            tables = introspect.read_schema(conn, chosen)
            kind = conn.kind
    except engine.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not tables:
        raise HTTPException(status_code=404, detail=f"No tables found in schema `{chosen}`.")
    return {"yaml_text": introspect.draft_yaml(tables, kind=kind), "table_count": len(tables)}


@router.post("/definitions/save")
def save_definitions(payload: SaveDefinitionsRequest) -> dict[str, Any]:
    """Store definitions, but only after they parse."""
    if not SESSION.allow_write:
        raise HTTPException(
            status_code=403, detail="This server was started read-only (--no-write)."
        )

    try:
        definitions = loads_definitions(payload.yaml_text, label="definitions")
    except DefinitionError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    with platform_db.session_scope() as session:
        version = store.save_definitions(
            session, payload.yaml_text, created_by="ui", workspace_id=tenancy.current()
        )
        number = version.version
    return {
        "saved": True,
        "version": number,
        "triggers": len(definitions.triggers),
        "rules": len(definitions.rules),
    }


def _current_yaml() -> str:
    """The definitions text in force. One source, no fallback."""
    with platform_db.session_scope() as session:
        active = store.active_definitions(session, workspace_id=tenancy.current())
        if active is not None:
            return active[0].yaml_text
    raise HTTPException(
        status_code=404,
        detail=(
            "No definitions stored yet. Generate a starting set from the "
            "schema, or import one with `rowfire platform push`."
        ),
    )


def _load_current() -> Definitions:
    try:
        return loads_definitions(_current_yaml(), label="definitions")
    except DefinitionError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc


@router.get("/definitions")
def get_definitions() -> dict[str, Any]:
    definitions = _load_current()
    return {
        "yaml_text": _current_yaml(),
        "triggers": [
            {
                "name": name,
                "description": trigger.description,
                "sql": trigger.sql,
                "event_time": trigger.event_time,
                "key": trigger.key,
                "source": trigger.source,
            }
            for name, trigger in sorted(definitions.triggers.items())
        ],
        "rules": [
            {
                "name": name,
                "trigger": rule.trigger,
                "description": rule.description,
                "policy": rule.policy.value,
                "period": rule.period,
                "n": rule.n,
            }
            for name, rule in sorted(definitions.rules.items())
        ],
    }


@router.get("/validate")
def validate() -> dict[str, Any]:
    """Check every trigger's query actually runs against the live schema."""
    definitions = _load_current()

    # One connection per source, each checking only the triggers that read it.
    by_source: dict[str | None, list[str]] = {}
    for name, trigger in definitions.triggers.items():
        by_source.setdefault(trigger.source, []).append(name)

    errors: list[str] = []
    for source, names in by_source.items():
        try:
            _, dsn = _source_dsn(source)
        except HTTPException as exc:
            errors.extend(f"trigger `{name}`: {exc.detail}" for name in names)
            continue
        dialect = sources.DIALECTS[sources.kind_of(dsn)]
        runnable = []
        for name in names:
            try:
                compile_trigger(definitions, name, dialect=dialect)
                runnable.append(name)
            except CompileError as exc:
                errors.extend(f"trigger `{name}`: {e}" for e in exc.errors)
        try:
            with engine.connect(dsn, SESSION.statement_timeout_ms) as conn:
                errors.extend(introspect.validate_definitions(definitions, conn, only=runnable))
        except engine.EngineError as exc:
            errors.extend(f"trigger `{name}`: {exc}" for name in runnable)

    return {"valid": not errors, "errors": errors}


@router.post("/backtest")
def backtest(payload: BacktestRequest) -> dict[str, Any]:
    definitions = _load_current()
    rule = definitions.rules.get(payload.rule)
    # The rule reads its trigger's source. An unknown rule falls through to
    # engine.run, which says so with the list of rules that do exist.
    source = _trigger_source(definitions, rule.trigger) if rule is not None else None
    dsn = _require_dsn(source)

    try:
        result = engine.run(
            definitions,
            payload.rule,
            days=payload.days,
            sample=payload.sample,
            dsn=dsn,
        )
    except CompileError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    except engine.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return serialise_result(result)


# --------------------------------------------------------------- authoring
#
# The composer edits one trigger or one rule at a time, but every write still
# goes through store.save_definitions. That is not indirection for its own
# sake: versioning, the definition checksum, and demote-on-edit all live
# there. A composer that wrote rows directly would silently lose all three.


class TriggerDraft(BaseModel):
    """A trigger as the composer builds it: a query, a clock, and a grain."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = None
    sql: str = Field(min_length=1)
    event_time: str | None = None
    key: list[str] = Field(min_length=1)
    # The data source it reads. Omitted means the default one.
    source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")

    def to_yaml_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.description:
            body["description"] = self.description
        if self.source:
            body["source"] = self.source
        body["sql"] = self.sql
        if self.event_time:
            body["event_time"] = self.event_time
        body["key"] = self.key
        return body


class SqlCheck(BaseModel):
    """A query being written, before it is a trigger.

    `event_time` and `key` are optional on purpose: the composer runs this to
    *discover* the output columns, and cannot name a clock or a grain until it
    knows what the query returns.
    """

    sql: str = Field(min_length=1)
    event_time: str | None = None
    key: list[str] = Field(default_factory=list)
    # Checked against this source, in its dialect. Omitted means the default.
    source: str | None = None


class RuleDraft(BaseModel):
    """A rule: which trigger it reads, and how often it may fire per key."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    trigger: str = Field(min_length=1)
    description: str | None = None
    policy: Literal["once_ever", "once_per_period", "once_per_n"] = "once_ever"
    period: Literal["day", "week", "month"] | None = None
    n: int | None = None

    def to_yaml_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"trigger": self.trigger}
        if self.description:
            body["description"] = self.description
        body["policy"] = self.policy
        if self.policy == "once_per_period":
            body["period"] = self.period or "day"
        if self.policy == "once_per_n":
            body["n"] = self.n or 2
        return body


class BindingDraft(BaseModel):
    rule_name: str
    integration: str
    action: str
    parameters: dict[str, str] = Field(default_factory=dict)
    # What the frequency cap counts against. Left empty it is the integration
    # and action as a whole, which caps the *channel* -- fine for an ops
    # firehose, wrong for anything addressed to a person. `{{ customer_id }}`
    # makes the cap per customer, which is what "at most one message a week"
    # nearly always means.
    recipient_template: str | None = None


class IntegrationDraft(BaseModel):
    """A named connection to an outside system, with its credentials.

    `provider` names a catalogue template to start from. Without one this is a
    fully hand-written integration, and everything it needs is in this
    request -- which is the case that has to work for the format to be
    genuinely open.
    """

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    provider: str | None = None
    base_url: str = ""
    auth_kind: Literal["none", "bearer", "header", "basic"] = "none"
    auth_header_name: str | None = None
    auth_credential: str = "token"
    # For basic auth: which credential is the username half.
    auth_username_credential: str | None = None
    # Write-only. These are sealed on arrival and never returned by any
    # endpoint; the listing reports which keys exist, never their values.
    credentials: dict[str, str] = Field(default_factory=dict)


class ParameterDraft(BaseModel):
    """One value the action asks for, and how to ask for it."""

    label: str | None = None
    type: Literal["string", "number", "boolean", "json"] = "string"
    help: str | None = None
    required: bool = True
    default: Any = None


class ActionDraft(BaseModel):
    """One REST call: what it needs, and the request it builds."""

    description: str | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str = ""
    # Declared, not inferred. Left empty, the placeholders in the templates
    # are used instead, so a quick edit does not have to restate everything.
    parameters: dict[str, ParameterDraft] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    retry_on: list[int] = Field(default_factory=list)


class _BlockDumper(yaml.SafeDumper):
    """A YAML dumper that writes multi-line strings as block scalars.

    Without this a composed trigger's SQL comes back out of `platform pull` as
    one quoted line full of `\n` escapes. That is valid YAML and completely
    unreadable, which matters because exporting to a file is now a deliberate
    act -- usually to put the definitions under version control, where a
    one-line diff on a wall of escapes is worthless.
    """


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    # Block scalars cannot preserve trailing whitespace on a line, so a string
    # carrying any is written the ordinary way rather than silently altered.
    if "\n" in data and not any(line != line.rstrip() for line in data.splitlines()):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _represent_str)


def _write_definitions(mutate: Any, *, created_by: str = "composer") -> dict[str, Any]:
    """Apply a change to the definitions document and store a new version.

    Comments in the YAML do not survive a programmatic edit. That is the
    accepted cost of composing in a UI; the authoritative copy is the stored
    version, and every version is kept.
    """
    import yaml as _yaml

    raw = _yaml.safe_load(_current_yaml()) or {}
    raw.setdefault("triggers", {})
    raw.setdefault("rules", {})
    mutate(raw)
    text = _yaml.dump(
        raw, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )

    try:
        definitions = loads_definitions(text, label="definitions")
    except DefinitionError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    with platform_db.session_scope() as session:
        version = store.save_definitions(
            session, text, created_by=created_by, workspace_id=tenancy.current()
        )
        return {
            "version": version.version,
            "triggers": len(definitions.triggers),
            "rules": len(definitions.rules),
        }


def _require_write() -> None:
    if not SESSION.allow_write:
        raise HTTPException(status_code=403, detail="This server was started read-only.")


@router.post("/triggers/check")
def check_trigger(draft: SqlCheck) -> dict[str, Any]:
    """Validate a query without saving, and report the columns it returns.

    Two things happen here, and the order matters. The SQL is checked by the
    same sqlglot validator everything else uses, then run with LIMIT 0 against
    the live database so the output columns come from the cursor rather than
    from parsing. That is what lets the composer offer real column names for
    the clock and the key -- including columns produced by a join or an alias,
    which no amount of static analysis would get right.
    """
    from .definitions import Trigger

    dsn = _require_dsn(draft.source)
    dialect = sources.DIALECTS[sources.kind_of(dsn)]

    # key is required on the model but unused by describe_sql. A placeholder
    # keeps the probe honest: key problems are reported below, against the
    # columns the query actually returned, not as a model validation error
    # before the composer has had a chance to see them.
    try:
        trigger = Trigger(sql=draft.sql, event_time=draft.event_time, key=draft.key or ["_"])
    except Exception as exc:  # noqa: BLE001 -- pydantic, surfaced not raised
        return {"valid": False, "errors": [str(exc)], "columns": [], "sql": None}

    try:
        compiled = compile_trigger_preview(trigger, dialect)
    except CompileError as exc:
        return {"valid": False, "errors": exc.errors, "columns": [], "sql": None}

    # Two different failures, kept apart: not reaching the database is the
    # server's problem (400); the database rejecting the query is the common
    # case while someone is typing, and is reported as a result.
    try:
        with engine.connect(dsn, SESSION.statement_timeout_ms) as conn:
            try:
                columns = introspect.describe_trigger(conn, trigger)
            except (engine.EngineError, CompileError) as exc:
                reason = "; ".join(exc.errors) if isinstance(exc, CompileError) else str(exc)
                return {
                    "valid": False,
                    "errors": [reason.removeprefix("query failed: ").splitlines()[0][:300]],
                    "columns": [],
                    "sql": compiled.display_sql(),
                }
    except engine.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    errors: list[str] = []
    if draft.event_time and draft.event_time not in columns:
        errors.append(f"`{draft.event_time}` is not a column this query returns")
    for column in draft.key:
        if column not in columns:
            errors.append(f"key column `{column}` is not a column this query returns")
    if not draft.event_time:
        errors.append("pick the column that places a row in time")
    if not draft.key:
        errors.append("pick the columns that identify one row")

    return {
        "valid": not errors,
        "errors": errors,
        "columns": [{"name": name, "type": kind} for name, kind in columns.items()],
        "sql": compiled.display_sql(),
    }


def compile_trigger_preview(trigger: Any, dialect: str = "postgres") -> Any:
    """Compile a draft trigger against a 90-day window, for display only.

    Shows the row cap and the ORDER BY as well as the window, so what the
    composer reads is the shape of the query that will really run.
    """
    from datetime import timedelta

    from .compile import compile_query
    from .definitions import Source

    until = datetime.now(tz=UTC)
    return compile_query(
        trigger,
        since=until - timedelta(days=90),
        until=until,
        limit=Source().max_rows,
        dialect=dialect,
    )


@router.put("/triggers/{trigger_name}")
def upsert_trigger(trigger_name: str, draft: TriggerDraft) -> dict[str, Any]:
    """Create or replace one trigger.

    A rename carries the rules with it. Leaving them pointing at a name that
    no longer exists would fail validation and reject the whole write, which
    reads as "the composer is broken" rather than "that rename orphaned
    something".
    """
    _require_write()

    def mutate(raw: dict[str, Any]) -> None:
        if trigger_name != draft.name:
            raw["triggers"].pop(trigger_name, None)
            for rule in raw["rules"].values():
                if isinstance(rule, dict) and rule.get("trigger") == trigger_name:
                    rule["trigger"] = draft.name
        raw["triggers"][draft.name] = draft.to_yaml_dict()

    result = _write_definitions(mutate)
    return {"saved": True, "name": draft.name, **result}


@router.delete("/triggers/{trigger_name}")
def delete_trigger(trigger_name: str) -> dict[str, Any]:
    _require_write()

    def mutate(raw: dict[str, Any]) -> None:
        if trigger_name not in raw["triggers"]:
            raise HTTPException(status_code=404, detail=f"no trigger `{trigger_name}`")
        dependents = sorted(
            name
            for name, rule in raw["rules"].items()
            if isinstance(rule, dict) and rule.get("trigger") == trigger_name
        )
        if dependents:
            # Said plainly here rather than left to the cross-reference
            # validator, whose "unknown trigger" message would be baffling in
            # response to a delete.
            raise HTTPException(
                status_code=409,
                detail={
                    "errors": [
                        f"`{trigger_name}` still feeds "
                        f"{len(dependents)} rule(s): {', '.join(dependents)}. "
                        "Delete or repoint them first."
                    ]
                },
            )
        del raw["triggers"][trigger_name]

    return {"deleted": True, **_write_definitions(mutate)}


@router.put("/rules/{rule_name}")
def upsert_rule(rule_name: str, draft: RuleDraft) -> dict[str, Any]:
    """Create or replace one rule."""
    _require_write()

    def mutate(raw: dict[str, Any]) -> None:
        if rule_name != draft.name:
            raw["rules"].pop(rule_name, None)
        raw["rules"][draft.name] = draft.to_yaml_dict()

    result = _write_definitions(mutate)
    return {"saved": True, "name": draft.name, **result}


@router.delete("/rules/{rule_name}")
def delete_rule(rule_name: str) -> dict[str, Any]:
    _require_write()

    def mutate(raw: dict[str, Any]) -> None:
        if rule_name not in raw["rules"]:
            raise HTTPException(status_code=404, detail=f"no rule `{rule_name}`")
        del raw["rules"][rule_name]

    return {"deleted": True, **_write_definitions(mutate)}


# ----------------------------------------------------- integrations
#
# Two entities, and the split is the whole point.
#
#   integration -- a named, configured connection to an outside system, with
#                  its credentials. "Acme Slack" is one; a second Slack
#                  workspace would be another.
#   action      -- one REST call on that integration, described as data:
#                  method, path, headers, body.
#
# Both are editable here, because an integration you can only add by writing
# Python is the thing this design exists to avoid.


def _serialise_parameters(action: Any) -> list[dict[str, Any]]:
    """The declared contract, in the order the UI should ask for it.

    Required first, then alphabetically, so a form reads sensibly rather than
    however a dict happened to iterate.
    """
    from .platform.integrations import action_parameters

    declared = action_parameters(action)
    ordered = sorted(declared.items(), key=lambda kv: (not kv[1].required, kv[0]))
    return [
        {
            "name": name,
            "label": spec.display(name),
            "type": spec.type.value,
            "help": spec.help,
            "required": spec.required,
            "default": spec.default,
        }
        for name, spec in ordered
    ]


def _serialise_action(action: Any) -> dict[str, Any]:
    parameters = _serialise_parameters(action)
    return {
        "id": str(action.id),
        "name": action.name,
        "description": action.description,
        "method": action.method,
        "path": action.path_template,
        "headers": action.headers_template,
        "body": action.body_template,
        "retry_on": action.retry_on,
        # The full declaration: name, label, type, help, required.
        "parameters": parameters,
        # Just the names, for callers that only need the list.
        "parameter_names": [p["name"] for p in parameters],
    }


def _serialise_integration(integration: Any, actions: list[Any]) -> dict[str, Any]:
    return {
        "id": str(integration.id),
        "name": integration.name,
        "description": integration.description,
        "provider": integration.provider,
        "base_url": integration.base_url,
        "auth_kind": integration.auth_kind.value,
        "auth_header_name": integration.auth_header_name,
        "auth_credential": integration.auth_credential,
        "auth_username_credential": integration.auth_username_credential,
        # Which credentials are set, never their values. The UI shows "set"
        # or "not set"; nothing here can leak one back to a browser.
        "credential_keys": sorted(_credential_keys(integration)),
        "enabled": integration.enabled,
        "timeout_ms": integration.timeout_ms,
        "actions": [_serialise_action(a) for a in sorted(actions, key=lambda a: a.name)],
    }


def _credential_keys(integration: Any) -> list[str]:
    """The names of the credentials held, with no values.

    A failure to decrypt is reported as "none stored" rather than raised: a
    rotated master key should make the UI say the integration needs
    reconnecting, not return a 500 from a listing endpoint.
    """
    from .platform.integrations import reveal_credentials

    try:
        return list(reveal_credentials(integration))
    except Exception:  # noqa: BLE001
        return []


@router.get("/catalogue")
def catalogue() -> dict[str, Any]:
    """The systems we already know how to talk to.

    Only ever a head start: everything here can equally be typed in by hand,
    and what it produces is the same rows.
    """
    from .platform.integrations import IntegrationError, available, load_template

    entries = []
    for name in available():
        try:
            template = load_template(name)
        except IntegrationError:
            continue
        entries.append(
            {
                "name": template.name,
                "description": template.description,
                "base_url": template.base_url,
                "auth_kind": template.auth.kind.value,
                "auth_credential": template.auth.credential,
                "auth_username_credential": template.auth.username_credential,
                "credentials": [
                    {
                        "key": key,
                        "label": spec.label,
                        "help": spec.help,
                        "secret": spec.secret,
                        "required": spec.required,
                    }
                    for key, spec in template.credentials.items()
                ],
                "actions": [
                    {
                        "name": action_name,
                        "description": spec.description,
                        "method": spec.method,
                        "path": spec.path,
                        "parameters": [
                            {
                                "name": parameter_name,
                                "label": parameter.display(parameter_name),
                                "type": parameter.type.value,
                                "help": parameter.help,
                                "required": parameter.required,
                                "default": parameter.default,
                            }
                            for parameter_name, parameter in spec.parameters.items()
                        ],
                    }
                    for action_name, spec in template.actions.items()
                ],
            }
        )
    return {"catalogue": entries}


@router.get("/integrations")
def list_integrations() -> dict[str, Any]:
    """Configured integrations and the actions each exposes."""
    _require_platform()
    from .platform.models import Action, Integration

    with platform_db.session_scope() as session:
        rows = session.exec(
            select(Integration).where(Integration.workspace_id == tenancy.current())
        ).all()
        out = []
        for integration in sorted(rows, key=lambda i: i.name):
            actions = session.exec(
                select(Action).where(Action.integration_id == integration.id)
            ).all()
            out.append(_serialise_integration(integration, list(actions)))
        return {"integrations": out}


@router.post("/integrations")
def create_integration(draft: IntegrationDraft) -> dict[str, Any]:
    """Create or update one integration.

    `provider` picks a catalogue template to start from, which prefills the
    base URL, the auth shape and a set of actions. Omit it (or pass "custom")
    and everything comes from this request instead -- the two paths produce
    the same row.
    """
    _require_platform()
    _require_write()
    from .platform.integrations import (
        AuthSpec,
        IntegrationError,
        IntegrationTemplate,
        install,
        load_template,
    )

    try:
        if draft.provider and draft.provider != "custom":
            template = load_template(draft.provider)
        else:
            template = IntegrationTemplate(
                name="custom",
                description=draft.description,
                base_url=draft.base_url or "",
                auth=AuthSpec(
                    kind=AuthKind(draft.auth_kind),
                    header_name=draft.auth_header_name,
                    credential=draft.auth_credential,
                    username_credential=draft.auth_username_credential,
                ),
            )

        with platform_db.session_scope() as session:
            integration = install(
                session,
                template,
                name=draft.name,
                credentials=dict(draft.credentials),
                base_url=draft.base_url if draft.base_url else None,
                description=draft.description,
                workspace_id=tenancy.current(),
            )
            created = str(integration.id)
    except IntegrationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    return {"created": True, "id": created, "name": draft.name}


@router.delete("/integrations/{integration_id}")
def delete_integration(integration_id: str) -> dict[str, Any]:
    """Remove an integration, its actions and anything bound to them."""
    _require_platform()
    _require_write()

    with platform_db.session_scope() as session:
        integration = _owned_integration(session, integration_id)
        session.delete(integration)
    return {"deleted": True}


@router.put("/integrations/{integration_id}/actions/{action_name}")
def upsert_integration_action(
    integration_id: str, action_name: str, draft: ActionDraft
) -> dict[str, Any]:
    """Define one REST call on an integration.

    This is the path that makes the product open-ended: method, path, headers
    and body, with `{{ placeholders }}` for whatever the binding supplies.
    """
    _require_platform()
    _require_write()
    from .platform.integrations import ActionSpec, IntegrationError, upsert_action

    try:
        spec = ActionSpec(
            description=draft.description,
            method=draft.method,
            path=draft.path,
            parameters={
                name: parameter.model_dump() for name, parameter in draft.parameters.items()
            },
            body=draft.body,
            headers=draft.headers,
            retry_on=draft.retry_on,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic, surfaced not raised
        raise HTTPException(status_code=422, detail={"errors": [str(exc)]}) from exc

    try:
        with platform_db.session_scope() as session:
            integration = _owned_integration(session, integration_id)
            action = upsert_action(session, integration, action_name, spec)
            result = _serialise_action(action)
    except IntegrationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    return {"saved": True, "action": result}


@router.delete("/integrations/{integration_id}/actions/{action_name}")
def delete_integration_action(integration_id: str, action_name: str) -> dict[str, Any]:
    _require_platform()
    _require_write()
    from .platform.models import Action

    with platform_db.session_scope() as session:
        integration = _owned_integration(session, integration_id)
        action = session.exec(
            select(Action).where(
                Action.integration_id == integration.id,
                Action.name == action_name,
            )
        ).first()
        if action is None:
            raise HTTPException(status_code=404, detail="no such action")
        session.delete(action)
    return {"deleted": True}


def _owned_integration(session: Any, integration_id: str) -> Any:
    """An integration in this workspace, or a 404.

    Addressed by id, so the id alone must not be enough: in a hosted demo
    another visitor's integration has an id too, and it is answered exactly
    like one that does not exist.
    """
    from .platform.models import Integration

    integration = session.get(Integration, _as_uuid(integration_id))
    if integration is None or integration.workspace_id != tenancy.current():
        raise HTTPException(status_code=404, detail="no such integration")
    return integration


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not a valid id") from exc


@router.get("/bindings")
def list_bindings() -> dict[str, Any]:
    _require_platform()
    from .platform.models import Action, Integration, RuleBinding

    with platform_db.session_scope() as session:
        bindings = session.exec(
            select(RuleBinding).where(RuleBinding.workspace_id == tenancy.current())
        ).all()
        out = []
        for binding in bindings:
            action = session.get(Action, binding.action_id)
            integration = session.get(Integration, action.integration_id) if action else None
            out.append(
                {
                    "id": str(binding.id),
                    "rule_name": binding.rule_name,
                    "integration": integration.name if integration else "?",
                    "action": action.name if action else "?",
                    "parameters": binding.parameters,
                    "recipient_template": binding.recipient_template,
                    "enabled": binding.enabled,
                }
            )
        return {"bindings": out}


@router.post("/bindings")
def create_binding(draft: BindingDraft) -> dict[str, Any]:
    _require_platform()
    from .platform.integrations import IntegrationError, bind

    try:
        with platform_db.session_scope() as session:
            binding = bind(
                session,
                draft.rule_name,
                draft.integration,
                draft.action,
                dict(draft.parameters),
                recipient_template=draft.recipient_template or None,
                workspace_id=tenancy.current(),
            )
            created = str(binding.id)
    except IntegrationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    return {"created": True, "id": created}


@router.delete("/bindings/{binding_id}")
def delete_binding(binding_id: str) -> dict[str, Any]:
    _require_platform()
    from .platform.models import RuleBinding

    with platform_db.session_scope() as session:
        binding = session.get(RuleBinding, _as_uuid(binding_id))
        if binding is None or binding.workspace_id != tenancy.current():
            raise HTTPException(status_code=404, detail="no such binding")
        session.delete(binding)
    return {"deleted": True}


# ------------------------------------------------------------ live control


class ModeRequest(BaseModel):
    mode: Literal["shadow", "live"]


class HaltRequest(BaseModel):
    reason: str = Field(min_length=1)


def _require_platform() -> None:
    """Refuse the request while the control plane is unreachable.

    This used to mean "you are running without a control plane", which was a
    supported mode. It cannot mean that now -- the server will not start
    without one -- so it means the plane has gone away since, and the honest
    status is 503 rather than 501: the request is implemented and would work
    again on retry.
    """
    if not platform_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "The control plane is not answering. Definitions, connections "
                "and fire history all live there, so nothing can be read or "
                "changed until it is back."
            ),
        )


def _fire_totals(session: Any, workspace_id: uuid.UUID) -> dict[str, tuple[int, Any]]:
    """Fires per rule, in one grouped query rather than one query per rule."""
    return {
        name: (fires, last)
        for name, fires, last in session.execute(
            sa_text(
                "SELECT rule_name, count(*), max(fired_at) FROM fire "
                "WHERE workspace_id = :ws GROUP BY rule_name"
            ),
            {"ws": workspace_id},
        ).all()
    }


@router.get("/triggers")
def trigger_states() -> dict[str, Any]:
    """Scheduling state per trigger: how far it has got, and when it runs next.

    Whether anything is *live* is not here -- that belongs to the rules, which
    are promoted one at a time. A trigger is a query on a clock.
    """
    _require_platform()
    with platform_db.session_scope() as session:
        workspace = store.ensure_workspace(session, tenancy.current())
        states = session.exec(
            select(TriggerState).where(TriggerState.workspace_id == workspace.id)
        ).all()
        rules = session.exec(select(RuleState).where(RuleState.workspace_id == workspace.id)).all()

        # Disabled rules are tombstones -- reconcile disables rather than
        # deletes them, so the ledger they wrote keeps its meaning. They are
        # not dependents of the trigger any more, so they are not counted here.
        per_trigger: dict[str, list[RuleState]] = {}
        for rule in rules:
            if rule.enabled:
                per_trigger.setdefault(rule.trigger_name, []).append(rule)

        totals = _fire_totals(session, workspace.id)

        return {
            "halted": workspace.halted,
            "halted_reason": workspace.halted_reason,
            "triggers": [
                {
                    "name": state.trigger_name,
                    "enabled": state.enabled,
                    "watermark": state.watermark.isoformat() if state.watermark else None,
                    "next_run_at": state.next_run_at.isoformat() if state.next_run_at else None,
                    "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
                    "last_error": state.last_error,
                    "poll_interval_seconds": state.poll_interval_seconds,
                    "lookback_seconds": state.lookback_seconds,
                    "rules": sorted(r.rule_name for r in per_trigger.get(state.trigger_name, [])),
                    "live_rules": sorted(
                        r.rule_name
                        for r in per_trigger.get(state.trigger_name, [])
                        if r.mode is Mode.live
                    ),
                    "total_fires": sum(
                        totals.get(r.rule_name, (0, None))[0]
                        for r in per_trigger.get(state.trigger_name, [])
                    ),
                }
                for state in sorted(states, key=lambda s: s.trigger_name)
            ],
        }


class RewindRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=3650)


@router.post("/triggers/{trigger_name}/run")
def run_trigger_now(trigger_name: str) -> dict[str, Any]:
    """Ask for this trigger to be polled on the next worker tick.

    Only moves the clock forward -- it does not widen the window, so this
    finds whatever has appeared since the last poll and nothing more. Safe to
    press repeatedly: the ledger still decides what actually fires.
    """
    _require_platform()
    from .platform.models import TriggerState

    with platform_db.session_scope() as session:
        state = session.exec(
            select(TriggerState).where(
                TriggerState.workspace_id == tenancy.current(),
                TriggerState.trigger_name == trigger_name,
            )
        ).first()
        if state is None:
            raise HTTPException(status_code=404, detail=f"no trigger `{trigger_name}`")
        state.next_run_at = datetime.now(tz=UTC)
        state.updated_at = state.next_run_at
        session.add(state)
        return {"queued": True, "trigger": trigger_name}


@router.post("/demo/activity")
def simulate_activity() -> dict[str, Any]:
    """Add a burst of activity to the sample database, then poll everything.

    Demo deployments only: without both settings `demo_activity` describes,
    this is a 404, the same as a route that does not exist. Polling every
    trigger straight away is what makes the result show up in seconds rather
    than at the next scheduled poll.
    """
    _require_platform()
    _require_write()
    if not demo_activity.configured():
        raise HTTPException(status_code=404, detail="simulated activity is not configured here")
    if hosted.enabled() and not hosted.allow_simulation(tenancy.current()):
        raise HTTPException(
            status_code=429, detail="That is a lot of activity. Give it a minute and try again."
        )
    try:
        # A hosted visitor's activity goes into their own copy of the tables.
        demo_activity.run(schema=hosted.schema_for(tenancy.current()) if hosted.enabled() else None)
    except demo_activity.ActivityError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    from .platform.models import TriggerState

    now = datetime.now(tz=UTC)
    with platform_db.session_scope() as session:
        states = session.exec(
            select(TriggerState).where(
                TriggerState.workspace_id == tenancy.current(),
                TriggerState.enabled,  # type: ignore[arg-type]
            )
        ).all()
        for state in states:
            state.next_run_at = now
            state.updated_at = now
            session.add(state)
        queued = sorted(state.trigger_name for state in states)
    return {"ok": True, "queued": queued}


@router.post("/triggers/{trigger_name}/rewind")
def rewind_trigger(trigger_name: str, payload: RewindRequest) -> dict[str, Any]:
    """Move the watermark back, so the next poll re-reads older rows.

    What makes this safe to offer at all is that the fire ledger is separate
    from the watermark: rewinding re-*reads* history, it does not re-fire it.
    Anything already in the ledger is recognised and skipped. What you get is
    the rows a rule has never seen -- which is exactly what you want when
    testing a rule written after the data.

    It is still a live-fire control when a rule is live, which is why the UI
    says which rules would be affected before you press it.
    """
    _require_platform()
    from .platform.models import TriggerState

    with platform_db.session_scope() as session:
        state = session.exec(
            select(TriggerState).where(
                TriggerState.workspace_id == tenancy.current(),
                TriggerState.trigger_name == trigger_name,
            )
        ).first()
        if state is None:
            raise HTTPException(status_code=404, detail=f"no trigger `{trigger_name}`")
        moment = datetime.now(tz=UTC)
        state.watermark = moment - timedelta(days=payload.days)
        state.next_run_at = moment
        state.updated_at = moment
        session.add(state)
        return {
            "trigger": trigger_name,
            "watermark": state.watermark.isoformat(),
            "days": payload.days,
        }


@router.get("/rules")
def rule_states() -> dict[str, Any]:
    """Runtime state per rule: is it live, and what has it actually fired."""
    _require_platform()
    with platform_db.session_scope() as session:
        workspace = store.ensure_workspace(session, tenancy.current())
        states = session.exec(select(RuleState).where(RuleState.workspace_id == workspace.id)).all()
        clocks = {
            t.trigger_name: t
            for t in session.exec(
                select(TriggerState).where(TriggerState.workspace_id == workspace.id)
            ).all()
        }
        totals = _fire_totals(session, workspace.id)

        out = []
        for state in sorted(states, key=lambda s: s.rule_name):
            fires, last_fired = totals.get(state.rule_name, (0, None))
            clock = clocks.get(state.trigger_name)
            out.append(
                {
                    "name": state.rule_name,
                    "trigger": state.trigger_name,
                    "mode": state.mode.value,
                    "enabled": state.enabled,
                    # The watermark belongs to the trigger, but a rule is read
                    # as "how current am I", so it is surfaced here too.
                    "watermark": clock.watermark.isoformat() if clock and clock.watermark else None,
                    "next_run_at": clock.next_run_at.isoformat()
                    if clock and clock.next_run_at
                    else None,
                    "last_error": clock.last_error if clock else None,
                    "total_fires": fires,
                    "last_fired_at": last_fired.isoformat() if last_fired else None,
                }
            )

        return {
            "halted": workspace.halted,
            "halted_reason": workspace.halted_reason,
            "rules": out,
        }


@router.post("/rules/{rule_name}/mode")
def set_rule_mode(rule_name: str, payload: ModeRequest) -> dict[str, Any]:
    """Promote one rule to live, or return it to shadow."""
    _require_platform()
    try:
        with platform_db.session_scope() as session:
            state = store.set_mode(
                session, rule_name, Mode(payload.mode), workspace_id=tenancy.current()
            )
            result = {"name": state.rule_name, "mode": state.mode.value}
    except store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


class LimitsRequest(BaseModel):
    cap_per_recipient: int = Field(ge=1, le=100_000)
    cap_window_hours: int = Field(ge=1, le=24 * 30)


@router.get("/limits")
def get_limits() -> dict[str, Any]:
    """The frequency cap: how many sends one recipient may receive, and over what.

    Exposed because it is a safety net you have to be able to see and adjust.
    Left invisible it reads as the product silently dropping messages -- which
    is exactly how it looked before this endpoint existed.
    """
    _require_platform()
    with platform_db.session_scope() as session:
        workspace = store.ensure_workspace(session, tenancy.current())
        return {
            "cap_per_recipient": workspace.cap_per_recipient,
            "cap_window_hours": workspace.cap_window_hours,
        }


@router.post("/limits")
def set_limits(payload: LimitsRequest) -> dict[str, Any]:
    _require_platform()
    _require_write()
    with platform_db.session_scope() as session:
        workspace = store.ensure_workspace(session, tenancy.current())
        workspace.cap_per_recipient = payload.cap_per_recipient
        workspace.cap_window_hours = payload.cap_window_hours
        session.add(workspace)
        return {
            "cap_per_recipient": workspace.cap_per_recipient,
            "cap_window_hours": workspace.cap_window_hours,
        }


@router.post("/halt")
def halt_workspace(payload: HaltRequest) -> dict[str, Any]:
    """Kill switch. Stops every rule, above any individual mode."""
    _require_platform()
    with platform_db.session_scope() as session:
        store.halt(session, payload.reason, workspace_id=tenancy.current())
    return {"halted": True, "reason": payload.reason}


@router.post("/resume")
def resume_workspace() -> dict[str, Any]:
    _require_platform()
    with platform_db.session_scope() as session:
        store.resume(session, workspace_id=tenancy.current())
    return {"halted": False}


@router.get("/activity")
def activity(limit: int = 20) -> dict[str, Any]:
    """Recent runs and recent fires -- the answer to 'is it doing anything'."""
    _require_platform()
    limit = max(1, min(limit, 100))
    with platform_db.session_scope() as session:
        runs = session.exec(
            select(Run)
            .where(Run.workspace_id == tenancy.current())
            .order_by(Run.started_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()

        # What each run actually sent. A run has no mode of its own -- the
        # rules on it do -- so "did anything leave the building" is answered
        # by counting deliveries rather than by labelling the run.
        sent_by_run: dict[Any, int] = {}
        for run_id, count in session.execute(
            sa_text(
                "SELECT f.run_id, count(*) FROM delivery d "
                "JOIN fire f ON f.id = d.fire_id "
                "WHERE d.status = 'sent' AND f.run_id IS NOT NULL "
                "AND d.workspace_id = :ws "
                "GROUP BY f.run_id"
            ),
            {"ws": tenancy.current()},
        ).all():
            sent_by_run[run_id] = count
        fires = session.exec(
            select(Fire)
            .where(Fire.workspace_id == tenancy.current())
            .order_by(Fire.fired_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()

        return {
            "runs": [
                {
                    "trigger": run.trigger_name,
                    "status": run.status.value,
                    "sent": sent_by_run.get(run.id, 0),
                    "matched_rows": run.matched_rows,
                    "fires_new": run.fires_new,
                    "fires_suppressed": run.fires_suppressed,
                    "started_at": run.started_at.isoformat(),
                    "error": run.error,
                }
                for run in runs
            ],
            "fires": [
                {
                    "rule": fire.rule_name,
                    "entity_id": fire.entity_id,
                    "event_time": fire.event_time.isoformat() if fire.event_time else None,
                    "fired_at": fire.fired_at.isoformat(),
                }
                for fire in fires
            ],
        }


@router.get("/inbox")
def inbox(limit: int = 50) -> dict[str, Any]:
    """What rules have put in the Demo inbox, newest first.

    Read straight from `delivery`: a request to `inbox://` is delivered by
    being recorded, so the delivery log *is* the inbox. Shadow deliveries are
    included and labelled -- what a rule would post is the useful thing to
    see before promoting it.
    """
    _require_platform()
    from sqlalchemy import or_

    from .platform.dispatch import INBOX_SCHEME, is_inbox
    from .platform.models import Action, Delivery, Integration

    limit = max(1, min(limit, 200))
    with platform_db.session_scope() as session:
        inboxes = [
            i
            for i in session.exec(
                select(Integration).where(Integration.workspace_id == tenancy.current())
            ).all()
            if is_inbox(i.base_url)
        ]
        action_ids = [
            action.id
            for action in session.exec(
                select(Action).where(Action.integration_id.in_([i.id for i in inboxes]))  # type: ignore[attr-defined]
            ).all()
        ]
        # By action as well as by URL: a delivery whose template failed to
        # render never got a URL, and a rule failing silently is exactly what
        # the inbox should show.
        rows = session.exec(
            select(Delivery)
            .where(
                Delivery.workspace_id == tenancy.current(),
                or_(
                    Delivery.request_url.ilike(f"{INBOX_SCHEME}%"),  # type: ignore[union-attr]
                    Delivery.action_id.in_(action_ids),  # type: ignore[union-attr]
                ),
            )
            .order_by(Delivery.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
        installed = [i.name for i in inboxes]

        return {
            "installed": sorted(installed),
            "items": [_serialise_inbox_item(row) for row in rows],
        }


def _serialise_inbox_item(delivery: Any) -> dict[str, Any]:
    """One delivery, in the shape the Inbox page draws.

    The kind comes from the request path -- `/messages` or `/tickets` -- not
    from the integration, so a custom action pointed at the inbox with either
    path is drawn the same way, and anything else falls back to raw JSON.
    """
    path = (delivery.request_url or "").split("://", 1)[-1].partition("/")[2]
    kind = {"messages": "message", "tickets": "ticket"}.get(path.strip("/"), "other")
    return {
        "id": str(delivery.id),
        "rule": delivery.rule_name,
        "integration": delivery.channel,
        "mode": delivery.mode.value,
        "status": delivery.status.value,
        "reason": delivery.suppressed_reason or delivery.error,
        "kind": kind,
        "body": (delivery.rendered or {}).get("body"),
        "created_at": delivery.created_at.isoformat(),
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
    }


# ------------------------------------------------------------------- app


def _host_name(host_header: str) -> str:
    """The host part of a Host header: no port, no IPv6 brackets."""
    host = host_header.split(",")[0].strip()
    if host.startswith("["):  # bracketed IPv6
        return host[1 : host.index("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback(host_header: str) -> bool:
    """Allow only loopback Host headers.

    Without this, any page the user browses could POST to 127.0.0.1 and drive
    this server against their production replica. Binding to loopback alone
    does not prevent that -- the browser is already inside.
    """
    host = _host_name(host_header)
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


ALLOWED_HOSTS_ENV = "ROWFIRE_ALLOWED_HOSTS"


def _allowed_hosts() -> frozenset[str]:
    """Extra host names to accept, exactly as named. Empty unless set.

    For a forwarded port or a proxy, where the browser reaches this server
    under a name that is not loopback -- the public demo's own domain, set by
    `rowfire cloud`. Exact names, never
    patterns: the guard exists so that a page on some other name cannot drive
    this server, and a wildcard would hand that back to anyone who can serve a
    page under the same suffix.
    """
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


def _host_allowed(host_header: str, allowed: frozenset[str]) -> bool:
    return _is_loopback(host_header) or _host_name(host_header).lower() in allowed


# Open to a hosted visitor before they have a workspace: enough to load the
# page, say it is a hosted demo, and make one.
_OPEN_WITHOUT_SESSION = {f"{API_PREFIX}/health", f"{API_PREFIX}/session", f"{API_PREFIX}/catalogue"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class _VisitorSession:
    """Bind each hosted visitor's request to their own workspace.

    Pure ASGI rather than BaseHTTPMiddleware, so the workspace set here is in
    the same context the endpoint runs in, and is copied into the thread a
    sync endpoint uses. Installed only on a hosted demo; a local install acts
    on its one workspace and never sees a cookie.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(f"{API_PREFIX}/"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        # A cookie is sent by the browser whoever asks, so a state-changing
        # request must come from this site's own pages. SameSite=Lax already
        # withholds the cookie from most cross-site requests; this closes the
        # rest. No Origin at all is a non-browser client, which has no
        # victim's cookie to borrow.
        if scope["method"] not in _SAFE_METHODS and not _same_origin(headers):
            await _json(send, 403, {"detail": "Cross-site requests are refused."})
            return

        workspace_id = await _visitor_workspace(headers)
        if workspace_id is None:
            if path not in _OPEN_WITHOUT_SESSION:
                await _json(
                    send,
                    401,
                    {"detail": "No demo session. Reload the page.", "session_required": True},
                )
                return
            workspace_id = NO_WORKSPACE

        with tenancy.acting_as(workspace_id):
            await self.app(scope, receive, send)


def _same_origin(headers: dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True
    from urllib.parse import urlsplit

    return urlsplit(origin).netloc.lower() == headers.get("host", "").lower()


async def _visitor_workspace(headers: dict[str, str]) -> uuid.UUID | None:
    from http.cookies import CookieError, SimpleCookie

    import anyio

    try:
        cookie = SimpleCookie(headers.get("cookie", ""))
    except CookieError:
        return None
    morsel = cookie.get(hosted.COOKIE)
    workspace_id = hosted.verify(morsel.value if morsel else None)
    if workspace_id is None:
        return None

    def still_live() -> bool:
        with platform_db.session_scope() as session:
            return hosted.live(session, workspace_id)

    try:
        return workspace_id if await anyio.to_thread.run_sync(still_live) else None
    except Exception:  # noqa: BLE001 -- a broken control plane is reported by health
        return None


async def _json(send: Any, status: int, body: dict[str, Any]) -> None:
    import json

    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def create_app(session: Session | None = None, dev_origin: str | None = None) -> FastAPI:
    global SESSION
    if session is not None:
        SESSION = session

    app = FastAPI(
        title="Rowfire",
        description="Local surface for the Rowfire engine.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    allowed_hosts = _allowed_hosts()

    @app.middleware("http")
    async def guard_host(request: Request, call_next):
        if not _host_allowed(request.headers.get("host", ""), allowed_hosts):
            return JSONResponse(
                status_code=421,
                content={
                    "detail": "This server only accepts loopback requests. "
                    "It is a local tool, not a service."
                },
            )
        return await call_next(request)

    app.include_router(router)
    # Outside /api, at the exact URL registered on the Supabase OAuth app.
    app.add_api_route(SUPABASE_CALLBACK_PATH, supabase_callback, methods=["GET"])

    if hosted.enabled():
        # A public instance shares its sample database with every visitor.
        SESSION.statement_timeout_ms = min(
            SESSION.statement_timeout_ms, hosted.STATEMENT_TIMEOUT_MS
        )
        app.add_middleware(_VisitorSession)

    # In dev the UI runs on Vite's own port and proxies /api here, so no CORS
    # is needed. dev_origin exists for the case where someone runs them apart.
    if dev_origin:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[dev_origin],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI, dist: Path | None = None) -> None:
    """Serve the built React app when it exists.

    Absent in local dev (Vite serves it) and present in the Docker image,
    where the front end is built in a node stage and copied in.
    """
    dist = dist or Path(__file__).parent / "ui_dist"
    if not (dist / "index.html").exists():
        return

    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    root = dist.resolve()
    index = root / "index.html"
    page = _with_head_html(index.read_text(), os.environ.get("ROWFIRE_HEAD_HTML"))

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> Response:
        # Every page is a deep link -- /rules/<name>, /integrations/<name>/...
        # -- so any path that is not a file is answered with the app, which
        # routes on it. Two exceptions. An unknown /api path is a client bug
        # and gets a 404 rather than a page of HTML. And a path that resolves
        # outside the build (`..`) is never served, whatever is there.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (dist / full_path).resolve()
        if (
            full_path
            and candidate != index
            and candidate.is_relative_to(root)
            and candidate.is_file()
        ):
            return FileResponse(candidate)
        return HTMLResponse(page)


def _with_head_html(page: str, head_html: str | None) -> str:
    """The app's page, with ROWFIRE_HEAD_HTML added at the end of its <head>.

    One instance's own markup -- an analytics tag, say -- kept in that
    instance's environment rather than in the repository. Unset, the page is
    served exactly as built. It is the operator's HTML and goes in verbatim:
    whoever can set the environment can already run anything.
    """
    if not head_html or "</head>" not in page:
        return page
    at = page.index("</head>")
    return page[:at] + head_html + "\n" + page[at:]
