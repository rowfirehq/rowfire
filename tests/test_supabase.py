"""Supabase as a data source: the Management API path, end to end.

Supabase itself is not reachable from a test, so a small fake Management API
stands in for it: the read-only query endpoint runs what it is sent against
the fixture Postgres, as the read-only role, inside a read-only transaction,
with an *empty* search_path -- which is what makes an unqualified table fail
there the way the real endpoint says it does. Everything Rowfire does to a
query on its way out (the to_json wrapper, the type probe, `public.`
qualification, positional parameters) is therefore checked against a real
database rather than against a string.

The SQL-translation tests at the top need nothing running.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from rowfire import engine, introspect, sources, supabase
from rowfire.definitions import loads as loads_definitions

REF = "abcdefghijklmnopqrst"
PG_DSN = os.environ.get(
    "ROWFIRE_FIXTURE_DSN", "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture"
)
PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)


# ------------------------------------------------------- SQL translation


def test_named_placeholders_become_positional_and_reused_names_share_one() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    sql, values = supabase.to_positional(
        "SELECT * FROM t WHERE a >= %(since)s AND b < %(until)s AND c >= %(since)s "
        "AND d LIKE '%%x%%'",
        {"since": since, "until": 5},
    )
    assert sql == (
        "SELECT * FROM t WHERE a >= $1::timestamptz AND b < $2 AND c >= $1::timestamptz "
        "AND d LIKE '%x%'"
    )
    assert values == [since, 5]


def test_plain_sql_is_left_alone_when_there_are_no_params() -> None:
    assert supabase.to_positional("SELECT '100%%'", None) == ("SELECT '100%%'", [])


def test_a_missing_parameter_is_named() -> None:
    with pytest.raises(supabase.SupabaseError, match=":until"):
        supabase.to_positional("SELECT %(until)s", {})


def test_unqualified_tables_get_public_and_nothing_else_does() -> None:
    sql = supabase.qualify(
        "WITH recent AS (SELECT * FROM orders) "
        "SELECT r.id FROM recent r JOIN auth.users u ON u.id = r.user_id "
        "JOIN customers c ON c.id = r.customer_id, pg_class p, generate_series(1, 2) g"
    )
    assert "public.orders" in sql
    assert "public.customers" in sql
    assert "auth.users" in sql
    assert "public.recent" not in sql
    assert "public.pg_class" not in sql
    assert "public.generate_series" not in sql


def test_already_qualified_sql_is_returned_byte_for_byte() -> None:
    sql = "SELECT  id FROM public.orders  WHERE status = 4"
    assert supabase.qualify(sql) == sql


def test_qualifying_keeps_placeholders_and_literal_percents() -> None:
    out = supabase.qualify_bound(
        "SELECT * FROM orders WHERE note LIKE '%%x%%' AND t >= %(since)s", {"since": 1}
    )
    assert "public.orders" in out
    assert "'%%x%%'" in out
    assert "%(since)s" in out


def test_dsn_round_trip_and_bad_refs() -> None:
    dsn = supabase.make_dsn(REF, "0f" * 16)
    assert sources.kind_of(dsn) == "supabase"
    assert sources.DIALECTS["supabase"] == "postgres"
    target = supabase.parse_dsn(dsn)
    assert (target.project_ref, target.grant_id) == (REF, "0f" * 16)
    assert sources.summarise(dsn) == f"Supabase project {REF}"
    with pytest.raises(supabase.SupabaseError):
        supabase.parse_dsn("supabase://not-a-ref")


def test_a_bare_dsn_reads_the_personal_access_token(monkeypatch) -> None:
    monkeypatch.delenv(supabase.ACCESS_TOKEN_ENV, raising=False)
    with pytest.raises(sources.EngineError, match=supabase.ACCESS_TOKEN_ENV):
        with sources.connect(supabase.make_dsn(REF)):
            pass


# ------------------------------------------------------------ fake API


class FakeSupabase:
    """The handful of Management API endpoints Rowfire calls."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.token_requests: list[dict[str, str]] = []
        self.access_tokens = {"pat-token"}
        self.issued = 0
        self.expires_in = 3600

    def issue(self) -> dict[str, object]:
        self.issued += 1
        access = f"access-{self.issued}"
        self.access_tokens.add(access)
        return {
            "access_token": access,
            "refresh_token": f"refresh-{self.issued}",
            "expires_in": self.expires_in,
            "token_type": "Bearer",
        }

    def run(self, sql: str, parameters: list[object]) -> list[dict[str, object]]:
        import psycopg
        from psycopg.rows import dict_row

        self.queries.append(sql)
        # The endpoint's $n, as psycopg's named placeholders. Literal `%`
        # first, so the conversion cannot mistake one for a placeholder.
        named = re.sub(r"\$(\d+)", r"%(p\1)s", sql.replace("%", "%%"))
        values = {f"p{i}": value for i, value in enumerate(parameters, start=1)}
        with psycopg.connect(PG_DSN, row_factory=dict_row, autocommit=True) as conn:
            conn.execute("SET search_path = ''")
            conn.execute("SET TIME ZONE 'UTC'")
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                rows = conn.execute(named, values).fetchall()
        return rows


@pytest.fixture
def fake(monkeypatch):
    state = FakeSupabase()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep test output clean
            pass

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and header[7:] in state.access_tokens

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/v1/projects":
                if not self._authorised():
                    return self._send(401, {"message": "unauthorised"})
                return self._send(
                    200,
                    [
                        {
                            "id": REF,
                            "ref": REF,
                            "name": "Acme production",
                            "organization_slug": "acme",
                            "region": "eu-west-1",
                            "status": "ACTIVE_HEALTHY",
                        }
                    ],
                )
            self._send(404, {"message": "not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/v1/oauth/token":
                form = {k: v[-1] for k, v in parse_qs(self._body().decode()).items()}
                state.token_requests.append(form)
                if form.get("client_secret") != "test-secret":
                    return self._send(401, {"message": "bad client"})
                return self._send(201, state.issue())
            if path == f"/v1/projects/{REF}/database/query/read-only":
                if not self._authorised():
                    return self._send(401, {"message": "unauthorised"})
                payload = json.loads(self._body())
                try:
                    rows = state.run(payload["query"], payload.get("parameters") or [])
                except Exception as exc:  # noqa: BLE001 -- relayed as the API would
                    return self._send(400, {"message": str(exc).splitlines()[0]})
                return self._send(201, rows)
            self._send(404, {"message": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(supabase.API_URL_ENV, f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv(supabase.ACCESS_TOKEN_ENV, "pat-token")
    monkeypatch.setenv(supabase.CLIENT_ID_ENV, "2c0b61e7-0000-4000-8000-000000000000")
    monkeypatch.setenv(supabase.CLIENT_SECRET_ENV, "test-secret")
    supabase._TYPE_CACHE.clear()
    yield state
    server.shutdown()
    server.server_close()


def _fixture_reachable() -> bool:
    try:
        with sources.connect(PG_DSN) as conn:
            conn.fetch("SELECT 1 AS one")
        return True
    except sources.EngineError:
        return False


needs_fixture = pytest.mark.skipif(not _fixture_reachable(), reason="needs the fixture Postgres")


# --------------------------------------------------------- the source


@pytest.mark.db
@needs_fixture
def test_rows_come_back_typed_like_a_direct_connection(fake) -> None:
    sql = (
        "SELECT id, status, total_amount, completed_at, completed_at::date AS day, "
        "1.5::float8 AS ratio, 12.30::numeric AS amount, '50%%' AS pct, "
        "jsonb_build_object('score', 0.25, 'n', 3) AS meta, ARRAY[1.5, 2.5]::float8[] AS xs "
        "FROM orders WHERE completed_at IS NOT NULL AND id <= %(top)s ORDER BY id"
    )
    with sources.connect(PG_DSN) as direct:
        expected, expected_types = direct.fetch(sql, {"top": 20})
    with sources.connect(supabase.make_dsn(REF)) as conn:
        assert conn.kind == "supabase"
        rows, types = conn.fetch(sql, {"top": 20})

    assert rows == expected
    assert types == expected_types
    first = rows[0]
    assert isinstance(first["completed_at"], datetime) and first["completed_at"].tzinfo
    assert isinstance(first["day"], date)
    assert first["ratio"] == 1.5 and isinstance(first["ratio"], float)
    assert first["amount"] == Decimal("12.30")
    assert first["pct"] == "50%"
    assert first["meta"] == {"score": 0.25, "n": 3}
    assert isinstance(first["meta"]["score"], float)
    assert first["xs"] == [1.5, 2.5]


@pytest.mark.db
@needs_fixture
def test_a_query_whose_shape_changes_is_typed_again(fake) -> None:
    with sources.connect(supabase.make_dsn(REF)) as conn:
        conn.fetch("SELECT id FROM orders WHERE id = %(n)s", {"n": 1})
        # Same SQL text, but the cache is told the shape it last saw; a row
        # with another column must not lose it.
        key = next(iter(supabase._TYPE_CACHE))
        supabase._TYPE_CACHE[key] = (("stale", 23),)
        rows, types = conn.fetch("SELECT id FROM orders WHERE id = %(n)s", {"n": 1})
    assert rows == [{"id": 1}]
    assert types == {"id": "identifier"}


@pytest.mark.db
@needs_fixture
def test_a_steady_poll_is_one_call_once_types_are_known(fake) -> None:
    sql = "SELECT id, completed_at FROM orders WHERE completed_at >= %(since)s"
    since = datetime(2000, 1, 1, tzinfo=UTC)
    with sources.connect(supabase.make_dsn(REF)) as conn:
        conn.fetch(sql, {"since": since})
        first = len(fake.queries)
        conn.fetch(sql, {"since": since + timedelta(days=1)})
    assert first == 2  # the rows, then their types
    assert len(fake.queries) == 3


@pytest.mark.db
@needs_fixture
def test_an_empty_result_still_knows_its_columns(fake) -> None:
    with sources.connect(supabase.make_dsn(REF)) as conn:
        rows, types = conn.fetch("SELECT id, completed_at FROM orders WHERE false", {})
        described = conn.describe("SELECT id, completed_at FROM orders")
    assert rows == []
    assert types == {"id": "identifier", "completed_at": "timestamp"}
    assert described == types


@pytest.mark.db
@needs_fixture
def test_the_schema_reads_the_same_as_through_a_direct_connection(fake) -> None:
    with sources.connect(PG_DSN) as direct:
        expected = introspect.read_schema(direct, "public")
    with sources.connect(supabase.make_dsn(REF)) as conn:
        tables = introspect.read_schema(conn)
        info = conn.server_info()
    assert tables == expected
    assert info["read_only"] is True
    assert info["database"].startswith(REF)


@pytest.mark.db
@needs_fixture
def test_a_backtest_through_supabase_matches_one_through_postgres(fake) -> None:
    definitions = loads_definitions(
        """
version: 2
triggers:
  order_completed:
    sql: SELECT id, customer_id, completed_at FROM orders WHERE status = 4
    event_time: completed_at
    key: [id]
rules:
  tell_ops: { trigger: order_completed, policy: once_ever }
"""
    )
    now = datetime.now(UTC)
    direct = engine.run(definitions, "tell_ops", days=365, dsn=PG_DSN, now=now)
    remote = engine.run(definitions, "tell_ops", days=365, dsn=supabase.make_dsn(REF), now=now)
    assert remote.matched_rows == direct.matched_rows > 0
    assert remote.fires == direct.fires
    assert remote.per_day == direct.per_day
    assert remote.column_types == direct.column_types


@pytest.mark.db
@needs_fixture
def test_a_write_is_refused_by_the_endpoint_not_just_by_rowfire(fake) -> None:
    with sources.connect(supabase.make_dsn(REF)) as conn:
        with pytest.raises(sources.EngineError, match="read-only"):
            conn.fetch("SELECT id FROM orders FOR UPDATE", {})


def test_a_rejected_token_says_to_reconnect(fake, monkeypatch) -> None:
    monkeypatch.setenv(supabase.ACCESS_TOKEN_ENV, "revoked")
    with sources.connect(supabase.make_dsn(REF)) as conn:
        with pytest.raises(sources.EngineError, match="Reconnect"):
            conn.fetch("SELECT 1 AS one", {})


# ----------------------------------------------------------- OAuth grants


def _platform() -> bool:
    import sqlalchemy
    from sqlalchemy import text

    try:
        engine_ = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine_.connect() as conn:
            conn.execute(text("SELECT 1 FROM oauth_grant LIMIT 1"))
        engine_.dispose()
        return True
    except Exception:
        return False


needs_platform = pytest.mark.skipif(not _platform(), reason="needs the control plane")


@pytest.fixture
def platform(monkeypatch):
    import sqlalchemy
    from sqlalchemy import text

    from rowfire import api
    from rowfire.platform import crypto, db

    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    db.reset_engine()
    api.reset_platform_cache()
    engine_ = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine_.begin() as conn:
        conn.execute(
            text(
                """
                DO $$
                DECLARE t text;
                BEGIN
                  FOR t IN
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                  LOOP
                    EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', t);
                  END LOOP;
                END $$;
                """
            )
        )
    engine_.dispose()
    yield
    api.reset_platform_cache()
    db.reset_engine()


@pytest.mark.db
@needs_platform
def test_an_expired_token_is_refreshed_once_and_the_new_one_kept(fake, platform) -> None:
    from rowfire.platform import db, oauth

    with db.session_scope() as session:
        grant = oauth.save_grant(
            session,
            supabase.Tokens(
                access_token="stale",
                refresh_token="refresh-0",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        )
        grant_id = grant.id.hex

    first = oauth.access_token(grant_id)
    second = oauth.access_token(grant_id)
    assert first == second == "access-1"
    assert [r["grant_type"] for r in fake.token_requests] == ["refresh_token"]
    assert fake.token_requests[0]["refresh_token"] == "refresh-0"

    with db.session_scope() as session:
        stored = oauth.reveal(oauth.get_grant(session, grant_id))
    assert stored.refresh_token == "refresh-1"  # the rotated one, durable


@pytest.mark.db
@needs_platform
def test_the_flow_cookie_is_opaque_and_tamper_evident(platform) -> None:
    from rowfire.platform import oauth

    sealed = oauth.seal_flow({"state": "s", "verifier": "very-secret"})
    assert "very-secret" not in base64.urlsafe_b64decode(sealed).decode()
    assert oauth.open_flow(sealed)["verifier"] == "very-secret"
    raw = json.loads(base64.urlsafe_b64decode(sealed))
    raw["c"] = base64.urlsafe_b64encode(b"x" * 40).decode()
    forged = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
    with pytest.raises(oauth.GrantError):
        oauth.open_flow(forged)


@pytest.mark.db
@needs_platform
@needs_fixture
def test_connect_supabase_end_to_end_through_the_api(fake, platform) -> None:
    from fastapi.testclient import TestClient

    from rowfire import api

    app = api.create_app(session=api.Session())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/sources/supabase").json()["oauth"] is True

        started = client.post("/api/sources/supabase/authorize", json={"name": "primary"})
        assert started.status_code == 200
        url = urlsplit(started.json()["url"])
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        assert url.path == "/v1/oauth/authorize"
        assert query["redirect_uri"] == "http://127.0.0.1/oauth/supabase/callback"
        assert query["code_challenge_method"] == "S256"

        # A callback with the wrong state is refused, and stores nothing.
        wrong = client.get(
            "/oauth/supabase/callback",
            params={"code": "c", "state": "forged"},
            follow_redirects=False,
        )
        assert wrong.status_code == 303 and "supabase_error" in wrong.headers["location"]
        assert fake.token_requests == []

        started = client.post("/api/sources/supabase/authorize", json={"name": "primary"})
        state = parse_qs(urlsplit(started.json()["url"]).query)["state"][-1]
        back = client.get(
            "/oauth/supabase/callback",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )
        assert back.status_code == 303
        location = parse_qs(urlsplit(back.headers["location"]).query)
        assert location["name"] == ["primary"]
        grant = location["supabase_grant"][-1]
        exchange = fake.token_requests[-1]
        assert exchange["grant_type"] == "authorization_code"
        assert exchange["code"] == "the-code"
        assert exchange["code_verifier"]  # PKCE, verified by the real server

        projects = client.get("/api/sources/supabase/projects", params={"grant": grant}).json()
        assert projects["projects"][0]["ref"] == REF

        added = client.post(
            "/api/sources/supabase",
            json={"name": "primary", "grant": grant, "project_ref": REF},
        )
        assert added.status_code == 200, added.text
        body = added.json()
        assert body["kind"] == "supabase"
        assert body["read_only"] is True
        assert body["table_count"] > 0
        assert body["host_summary"] == f"Supabase project {REF}"

        listed = client.get("/api/sources").json()["sources"]
        assert [(s["name"], s["label"]) for s in listed] == [("primary", "Supabase")]

        schema = client.get("/api/schema").json()
        assert "orders" in {t["name"] for t in schema["tables"]}

        # A starter file drafted from the project is one the server accepts.
        draft = client.post("/api/definitions/draft").json()["yaml_text"]
        assert "type: supabase" in draft
        loads_definitions(draft)


@pytest.mark.db
@needs_platform
def test_a_grant_from_another_workspace_cannot_be_used(fake, platform) -> None:
    import uuid

    from fastapi.testclient import TestClient

    from rowfire import api
    from rowfire.platform import db, oauth

    with db.session_scope() as session:
        grant = oauth.save_grant(
            session,
            supabase.Tokens("access-x", "r", datetime.now(UTC) + timedelta(hours=1)),
            workspace_id=uuid.uuid4(),
        )
        grant_id = grant.id.hex

    app = api.create_app(session=api.Session())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        answer = client.get("/api/sources/supabase/projects", params={"grant": grant_id})
    assert answer.status_code == 404
