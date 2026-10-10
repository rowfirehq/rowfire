"""Supabase as a data source, through the Management API rather than a DSN.

A Supabase project can be read without anyone handing over a database
password. The Management API runs a query as `supabase_read_only_user`
(`POST /v1/projects/{ref}/database/query/read-only`), and an OAuth app with
nothing more than the `database:read` scope may call it. So the customer
clicks Connect, approves, picks a project, and that is the whole setup: no
role to create, no IP to allow, no IPv6 pooler to find.

A source of this kind is still a "DSN" to everything above `sources.py`:

    supabase://<project ref>?grant=<oauth grant id>   tokens from an OAuth grant
    supabase://<project ref>                          SUPABASE_ACCESS_TOKEN

The grant id names a row in the control plane (`platform/oauth.py`) that
holds the tokens encrypted and refreshes them. The bare form reads a personal
access token from the environment, for the CLI and for anyone running
Rowfire themselves without registering an OAuth app.

Read-only is enforced the same three ways as any other source: the role
(Supabase's own read-only user), the endpoint (which runs nothing else), and
the query checker. The third is unchanged; the first two replace the session
settings a direct connection uses.

What the HTTP hop costs, and how it is paid back:

  * Rows arrive as JSON, so their types are lost. Each query is wrapped in
    `to_json(...)`, which spells timestamps as ISO 8601 whatever the session
    settings, and the column types are asked for once with `pg_typeof` and
    cached per query text -- the compiled SQL of a trigger is the same on
    every poll, only its parameters change -- so a steady poll is one call.
  * The endpoint wants every table schema-qualified. Unqualified tables are
    qualified with `public` before the query is sent; CTE names and the
    `pg_` catalogue are left alone.
  * Parameters are positional (`$1`), where compiled SQL uses `%(name)s`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

API_URL_ENV = "ROWFIRE_SUPABASE_API_URL"
DEFAULT_API_URL = "https://api.supabase.com"
ACCESS_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
CLIENT_ID_ENV = "SUPABASE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "SUPABASE_OAUTH_CLIENT_SECRET"

SCHEME = "supabase"

# A project ref is twenty lowercase letters; anything else is not one, and is
# refused before it is put into a URL path.
_REF = re.compile(r"^[a-z]{20}$")

_TIMEOUT_SECONDS = 30
_ALIAS = "rowfire_q"


class SupabaseError(Exception):
    """A Management API problem. Never carries a token or a client secret."""

    def __init__(self, message: str, status: int | None = None, detail: str = "") -> None:
        self.status = status
        # Supabase's own reason, when it gave one, for callers that word the
        # error for their context rather than relaying the generic message.
        self.detail = detail
        super().__init__(message)


def api_url() -> str:
    return (os.environ.get(API_URL_ENV) or DEFAULT_API_URL).rstrip("/")


# ------------------------------------------------------------------ DSN


@dataclass(frozen=True)
class Target:
    """Which project a supabase:// source reads, and whose token it reads with."""

    project_ref: str
    grant_id: str | None = None


def parse_dsn(dsn: str) -> Target:
    parts = urllib.parse.urlsplit(dsn.strip())
    ref = (parts.netloc or parts.path.lstrip("/")).lower()
    if not _REF.match(ref):
        raise SupabaseError(
            "a Supabase source is supabase://<project ref>, where the ref is the "
            "20 letters in your project's URL"
        )
    query = urllib.parse.parse_qs(parts.query)
    grant = (query.get("grant") or [None])[-1]
    return Target(project_ref=ref, grant_id=grant or None)


def make_dsn(project_ref: str, grant_id: str | None = None) -> str:
    if not _REF.match(project_ref):
        raise SupabaseError("not a Supabase project ref")
    dsn = f"{SCHEME}://{project_ref}"
    return f"{dsn}?grant={grant_id}" if grant_id else dsn


def summarise(dsn: str) -> str:
    """A label for the UI. The grant id is not a secret, but it is noise."""
    try:
        return f"Supabase project {parse_dsn(dsn).project_ref}"
    except SupabaseError:
        return "Supabase project ?"


# ---------------------------------------------------------------- HTTP


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
) -> Any:
    """One call to the Management API, decoded. Errors never echo the request."""
    headers = {"Accept": "application/json", "User-Agent": "rowfire"}
    data: bytes | None = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        data = json.dumps(json_body, default=_json_param).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(
        f"{api_url()}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        raise SupabaseError(
            _http_message(exc.code, detail), status=exc.code, detail=detail
        ) from None
    except urllib.error.URLError as exc:
        raise SupabaseError(f"could not reach Supabase: {exc.reason}") from None
    except TimeoutError:
        raise SupabaseError("Supabase did not answer in time") from None

    if not body:
        return None
    # Decimal, so a numeric column keeps every digit; floats are put back by
    # type once the column types are known.
    return json.loads(body, parse_float=Decimal)


def _http_detail(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.loads(exc.read() or b"null")
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error") or "")
    except (ValueError, OSError):
        pass
    return detail.strip().split("\n")[0][:300]


def _http_message(code: int, detail: str) -> str:
    if code == 401:
        return "Supabase refused the token (401). Reconnect the Supabase source."
    if code == 403:
        return f"Supabase refused access to this project (403){': ' + detail if detail else ''}"
    if code == 429:
        return "Supabase rate-limited this request (429). Polls will retry on the next tick."
    return f"Supabase returned {code}{': ' + detail if detail else ''}"


def _json_param(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot send a {type(value).__name__} as a query parameter")


def run_read_only(project_ref: str, token: str, sql: str, parameters: list[Any]) -> list[dict]:
    """Run one query as Supabase's read-only user and return its rows."""
    if not _REF.match(project_ref):
        raise SupabaseError("not a Supabase project ref")
    body: dict[str, Any] = {"query": sql}
    if parameters:
        body["parameters"] = parameters
    rows = _request(
        "POST",
        f"/v1/projects/{project_ref}/database/query/read-only",
        token=token,
        json_body=body,
    )
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise SupabaseError("Supabase returned something other than rows")
    return rows


def list_projects(token: str) -> list[dict[str, Any]]:
    """Every project the token can see: ref, name, organization and status."""
    projects = _request("GET", "/v1/projects", token=token) or []
    return [
        {
            "ref": p.get("ref") or p.get("id"),
            "name": p.get("name"),
            "organization": p.get("organization_slug") or p.get("organization_id"),
            "region": p.get("region"),
            "status": p.get("status"),
        }
        for p in projects
        if isinstance(p, dict)
    ]


# --------------------------------------------------------------- OAuth


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str


def oauth_client() -> OAuthClient | None:
    """The OAuth app this server was configured with, or None."""
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        return None
    return OAuthClient(client_id=client_id, client_secret=client_secret)


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime

    def expired(
        self, now: datetime | None = None, margin: timedelta = timedelta(minutes=2)
    ) -> bool:
        return (now or datetime.now(UTC)) + margin >= self.expires_at

    def dumps(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat(),
            }
        )

    @classmethod
    def loads(cls, text: str) -> Tokens:
        raw = json.loads(text)
        return cls(
            access_token=raw["access_token"],
            refresh_token=raw.get("refresh_token"),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
        )


def pkce_pair() -> tuple[str, str]:
    """A PKCE (verifier, S256 challenge) pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client: OAuthClient, redirect_uri: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{api_url()}/v1/oauth/authorize?{query}"


def exchange_code(client: OAuthClient, code: str, verifier: str, redirect_uri: str) -> Tokens:
    return _token_request(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )


def refresh(client: OAuthClient, refresh_token: str) -> Tokens:
    tokens = _token_request(client, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    if tokens.refresh_token is None:
        # Not every server rotates; keep the one that still works.
        tokens.refresh_token = refresh_token
    return tokens


def _token_request(client: OAuthClient, fields: dict[str, str]) -> Tokens:
    try:
        payload = _request(
            "POST",
            "/v1/oauth/token",
            form={"client_id": client.client_id, "client_secret": client.client_secret, **fields},
        )
    except SupabaseError as exc:
        # The generic wording is about a project or a token; neither is what
        # went wrong here, and "this project" sends people to the wrong place.
        if exc.status is None or exc.status >= 500 or exc.status == 429:
            raise
        reason = f": {exc.detail}" if exc.detail else ""
        if "client" in exc.detail.lower():
            raise SupabaseError(
                f"Supabase rejected this server's OAuth app ({exc.status}){reason}. "
                f"Check {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV}.",
                status=exc.status,
                detail=exc.detail,
            ) from None
        raise SupabaseError(
            f"Supabase refused the token request ({exc.status}){reason}",
            status=exc.status,
            detail=exc.detail,
        ) from None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise SupabaseError("Supabase's token endpoint returned no access token")
    expires_in = int(payload.get("expires_in") or 3600)
    return Tokens(
        access_token=str(payload["access_token"]),
        refresh_token=payload.get("refresh_token"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


# --------------------------------------------------------------- tokens


def _platform_token(grant_id: str) -> str:
    from .platform import oauth

    return oauth.access_token(grant_id)


# Swappable for tests, and for anything that keeps grants somewhere else.
grant_token: Callable[[str], str] = _platform_token


def token_for(target: Target) -> str:
    if target.grant_id:
        return grant_token(target.grant_id)
    token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise SupabaseError(
            f"no Supabase token: connect the project with Supabase OAuth, or set "
            f"{ACCESS_TOKEN_ENV} to a personal access token"
        )
    return token


# ------------------------------------------------------- SQL translation

_PLACEHOLDER = re.compile(r"%\((\w+)\)s|%%")


def to_positional(sql: str, params: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """`%(name)s` placeholders as `$n`, with literal `%%` put back to `%`.

    A mapping -- even an empty one -- means the SQL is in bind form, the same
    contract `SourceConnection.fetch` has. Timestamps are cast explicitly, so
    `col >= $1` compares as a timestamp however the parameter is typed.
    """
    if params is None:
        return sql, []
    order: dict[str, int] = {}
    values: list[Any] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name is None:
            return "%"
        if name not in params:
            raise SupabaseError(f"query uses :{name}, which nothing supplied")
        if name not in order:
            values.append(params[name])
            order[name] = len(values)
        cast = "::timestamptz" if isinstance(params[name], datetime) else ""
        return f"${order[name]}{cast}"

    return _PLACEHOLDER.sub(replace, sql), values


def qualify(sql: str) -> str:
    """Prefix unqualified tables with `public`. Returned unchanged if none are."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return sql  # the endpoint will say what is wrong, in Postgres's words

    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    changed = False
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or table.args.get("db") is not None:
            continue
        lowered = name.lower()
        if lowered in ctes or lowered.startswith("pg_"):
            continue
        if not isinstance(table.this, exp.Identifier):
            continue  # a table function, such as generate_series(...)
        table.set("db", exp.to_identifier("public"))
        changed = True
    return tree.sql(dialect="postgres") if changed else sql


# ------------------------------------------------------------ connection

# pg_typeof OIDs whose JSON spelling needs turning back into a Python value.
_TIMESTAMP_OIDS = {1114, 1184}
_DATE_OID = 1082
_INT_OIDS = {20, 21, 23}
# numeric and numeric[]: kept as Decimal, as psycopg returns them.
_NUMERIC_OIDS = {1700, 1231}

# Column types per query, so a steady poll costs one call. Keyed by project
# and SQL text: the same trigger on another project is another entry.
_TYPE_CACHE: OrderedDict[tuple[str, str], tuple[tuple[str, int], ...]] = OrderedDict()
_TYPE_CACHE_SIZE = 256


class SupabaseConnection:
    """A `SourceConnection` look-alike that speaks to the Management API.

    The engine, scheduler, schema reader and API only ever call `fetch`,
    `describe`, `server_info`, `default_schema` and `rollback`, so this is
    all it needs to be.
    """

    kind = "supabase"
    dialect = "postgres"

    def __init__(self, target: Target, token: str) -> None:
        self.target = target
        self._token = token

    # Nothing is held open between calls, so there is nothing to roll back.
    def rollback(self) -> None:
        return None

    def cursor(self) -> Any:
        raise SupabaseError("a Supabase source has no cursor; use fetch()")

    def _run(self, sql: str, parameters: list[Any]) -> list[dict]:
        return run_read_only(self.target.project_ref, self._token, sql, parameters)

    def fetch(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        from .sources import EngineError

        try:
            positional, values = to_positional(qualify_bound(sql, params), params)
            wrapped = f"SELECT to_json({_ALIAS}) AS r FROM (\n{positional}\n) AS {_ALIAS}"
            raw = [row.get("r") or {} for row in self._run(wrapped, values)]
            types = self._types(positional, values, raw[0] if raw else None)
        except SupabaseError as exc:
            raise EngineError(f"query failed: {exc}") from None

        rows = [{name: _convert(row.get(name), oid) for name, oid in types} for row in raw]
        return rows, _semantic(types)

    def describe(self, sql: str) -> dict[str, str]:
        _, columns = self.fetch(sql, {})
        return columns

    def _types(
        self, sql: str, values: list[Any], sample: dict[str, Any] | None
    ) -> tuple[tuple[str, int], ...]:
        key = (self.target.project_ref, sql)
        cached = _TYPE_CACHE.get(key)
        # A row whose columns differ from the cached ones means the query's
        # shape changed underneath it -- a `SELECT *` over a table that gained
        # a column -- so the types are asked for again rather than the new
        # column silently dropped.
        if cached is not None and (sample is None or list(sample) == [n for n, _ in cached]):
            _TYPE_CACHE.move_to_end(key)
            return cached

        # Zero rows over a LEFT JOIN to one dummy row gives a row of NULLs that
        # still carries every column name and declared type, so this works
        # whether or not the window had anything in it. The projection is
        # wrapped once more because a whole-row reference to the null-extended
        # side of a join is itself NULL; one to `rowfire_z` is a real row.
        empty = (
            f"FROM (SELECT {_ALIAS}.* FROM (SELECT 1) AS rowfire_one LEFT JOIN (\n"
            f"SELECT * FROM (\n{sql}\n) AS rowfire_inner LIMIT 0\n"
            f") AS {_ALIAS} ON true) AS rowfire_z"
        )
        if sample is not None:
            names = list(sample)
        else:
            shape = self._run(f"SELECT to_json(rowfire_z) AS r {empty}", values)
            names = list((shape[0].get("r") or {}) if shape else {})
        if not names:
            return ()

        selects = ", ".join(
            f"pg_typeof(rowfire_z.{_quote(name)})::oid::int AS {_quote(f'c{i}')}"
            for i, name in enumerate(names)
        )
        typed = self._run(f"SELECT {selects} {empty}", values)
        oids = typed[0] if typed else {}
        result = tuple((name, int(oids.get(f"c{i}") or 0)) for i, name in enumerate(names))

        _TYPE_CACHE[key] = result
        if len(_TYPE_CACHE) > _TYPE_CACHE_SIZE:
            _TYPE_CACHE.popitem(last=False)
        return result

    def server_info(self) -> dict[str, Any]:
        rows, _ = self.fetch(
            "SELECT version() AS version, current_database() AS database, "
            "current_setting('transaction_read_only') AS read_only"
        )
        info = rows[0] if rows else {}
        return {
            "version": str(info.get("version", "")).split(" on ")[0],
            "database": f"{self.target.project_ref}/{info.get('database', '')}",
            "read_only": str(info.get("read_only")) == "on",
        }

    def default_schema(self) -> str:
        return "public"


def qualify_bound(sql: str, params: dict[str, Any] | None) -> str:
    """`qualify`, on SQL that may still hold `%(name)s` placeholders.

    sqlglot reads `%(name)s` back as a placeholder and re-renders it the same
    way, but not `%%`; those are swapped out around the parse.
    """
    if params is None:
        return qualify(sql)
    marker = "rowfire_percent_marker"
    return qualify(sql.replace("%%", marker)).replace(marker, "%%")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _convert(value: Any, oid: int) -> Any:
    if value is None:
        return None
    if oid in _TIMESTAMP_OIDS and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value  # 'infinity', which no datetime can hold
        # A `timestamp` column carries no zone; read it as UTC, the same rule
        # the MySQL source uses for DATETIME.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if oid == _DATE_OID and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    if oid in _INT_OIDS and isinstance(value, Decimal):
        return int(value)
    if oid in _NUMERIC_OIDS:
        return value
    # Everything else -- float columns, json, arrays -- gets floats back,
    # which is what psycopg returns for them and what json.dumps can send.
    return _floats(value)


def _floats(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _floats(item) for key, item in value.items()}
    return value


def _semantic(types: tuple[tuple[str, int], ...]) -> dict[str, str]:
    from .introspect import semantic_from_cursor

    @dataclass
    class _Column:  # the two attributes semantic_from_cursor reads
        name: str
        type_code: int

    return semantic_from_cursor([_Column(name, oid) for name, oid in types], "postgres")


def connect(dsn: str) -> SupabaseConnection:
    from .sources import EngineError

    try:
        target = parse_dsn(dsn)
        return SupabaseConnection(target, token_for(target))
    except SupabaseError as exc:
        raise EngineError(f"could not connect to Supabase: {exc}") from None
