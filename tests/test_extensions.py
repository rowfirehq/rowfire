"""Extensions: the contract between Rowfire and code that plugs into it.

A fake extension claims one host name, signs people in with a header, adds
a route and some health, and refuses events past a limit -- the same
questions a real one answers, asked through the real middleware and the
real ingest routes. If one of these breaks, every extension breaks with it.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import text

from rowfire import api, extensions, hosted, tenancy
from rowfire.platform import crypto, dispatch, store
from rowfire.platform import db as platform_db
from rowfire.platform import events as event_store
from test_hosted import FIXTURE_ADMIN_DSN, FIXTURE_DSN, PLATFORM_DSN, REPO_ROOT, _reachable

pytestmark = pytest.mark.skipif(not _reachable(), reason="databases unavailable")

OPEN = frozenset({"/api/health", "/api/ext/hello"})


class Fake(extensions.Extension):
    """Claims `ext.test`; whoever sends X-Workspace is signed in as it."""

    def __init__(self) -> None:
        self.limit: int | None = None
        self.stored: dict[uuid.UUID, int] = {}

    def resolve(self, headers):
        if headers.get("host") != "ext.test":
            return None
        raw = headers.get("x-workspace")
        return extensions.Resolution(
            realm="fake",
            workspace_id=uuid.UUID(raw) if raw else None,
            open_paths=OPEN,
            refusal={"detail": "Sign in, please."},
            signin_url="/fake-signin",
        )

    def routes(self):
        router = APIRouter(prefix="/api/ext")

        @router.get("/hello")
        def hello():
            return {"realm": tenancy.realm(), "workspace": tenancy.current().hex}

        return [router]

    def health(self, session, workspace_id, realm):
        return {"fake": {"workspace": workspace_id.hex}} if realm == "fake" else {}

    def may_store_events(self, session, workspace_id):
        if self.limit is not None and self.stored.get(workspace_id, 0) >= self.limit:
            return "over the fake limit"
        return None

    def events_stored(self, session, workspace_id, count):
        self.stored[workspace_id] = self.stored.get(workspace_id, 0) + count

    def may_poll(self, session, workspace_id):
        return "paused by the fake"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())
    monkeypatch.setenv(hosted.ENABLED_ENV, "1")
    monkeypatch.setenv("ROWFIRE_COOKIE_INSECURE", "1")
    monkeypatch.setenv("ROWFIRE_DEMO_DSN", FIXTURE_DSN)
    monkeypatch.setenv("ROWFIRE_DEMO_ACTIVITY_DSN", FIXTURE_ADMIN_DSN)
    monkeypatch.setenv("ROWFIRE_HOSTED_DEFINITIONS", str(REPO_ROOT / "definitions.yaml"))
    monkeypatch.setenv("ROWFIRE_ALLOWED_HOSTS", "demo.test,ext.test")
    monkeypatch.setenv("ROWFIRE_SERVE_INGEST", "1")
    monkeypatch.delenv("ROWFIRE_DEMO_MYSQL_DSN", raising=False)
    monkeypatch.setattr(hosted, "_sessions_by_address", hosted._Throttle())
    platform_db.reset_engine()
    api.reset_platform_cache()
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE workspace, ingest_key, ingested_event CASCADE"))
    engine.dispose()

    fake = Fake()
    extensions.use([fake])
    app = api.create_app(session=api.Session())
    clients: list[TestClient] = []

    def client(base: str, workspace: uuid.UUID | None = None) -> TestClient:
        headers = {"x-workspace": workspace.hex} if workspace else {}
        c = TestClient(app, base_url=base, headers=headers)
        c.__enter__()
        clients.append(c)
        return c

    yield client, fake

    for c in clients:
        c.__exit__(None, None, None)
    extensions.use(None)
    api.reset_platform_cache()
    platform_db.reset_engine()


def _workspace() -> uuid.UUID:
    workspace_id = uuid.uuid4()
    with platform_db.session_scope() as session:
        store.ensure_workspace(session, workspace_id)
    return workspace_id


def test_a_claimed_host_without_sign_in_reaches_only_its_open_paths(server) -> None:
    client, _ = server
    stranger = client("http://ext.test")
    health = stranger.get("/api/health").json()
    assert health["realm"] == "fake" and health["session"] is False
    assert health["hosted"] is False
    refused = stranger.get("/api/definitions")
    assert refused.status_code == 401
    assert refused.json() == {
        "detail": "Sign in, please.",
        "signin_required": True,
        "signin_url": "/fake-signin",
    }
    assert health["signin_url"] == "/fake-signin"
    assert stranger.post("/api/session").status_code == 401


def test_a_signed_in_request_acts_on_the_extensions_workspace(server) -> None:
    client, _ = server
    workspace_id = _workspace()
    member = client("http://ext.test", workspace_id)
    assert member.get("/api/ext/hello").json() == {"realm": "fake", "workspace": workspace_id.hex}
    health = member.get("/api/health").json()
    assert health["fake"] == {"workspace": workspace_id.hex}
    # Apps can send events to this same address, so Connect offers it.
    assert health["ingest_here"] is True
    assert health["demo_activity"] is False
    # Shared server: no database of one's own, but keys to push events with.
    assert (
        member.post("/api/sources", json={"name": "x", "dsn": "postgresql://h/d"}).status_code
        == 403
    )
    assert member.post("/api/app-events/keys", json={"name": "web"}).status_code == 200


def test_unclaimed_hosts_are_still_the_demo(server) -> None:
    client, _ = server
    visitor = client("http://demo.test")
    assert visitor.post("/api/session").status_code == 200
    health = visitor.get("/api/health").json()
    assert health["realm"] == "demo" and health["hosted"] is True
    assert health["signin_url"] is None and health["ingest_here"] is False
    assert "fake" not in health
    assert visitor.get("/api/app-events/keys").status_code == 404


def test_an_extension_counts_events_and_can_refuse_more(server) -> None:
    client, fake = server
    workspace_id = _workspace()
    with platform_db.session_scope() as session:
        _, key = event_store.create_key(session, "web", workspace_id=workspace_id)
    app = client("http://ext.test")
    headers = {"authorization": f"Bearer {key}"}

    first = {"events": [{"id": "evt_00000001", "subscription": "refund"}]}
    assert app.post("/v1/events", json=first, headers=headers).status_code == 202
    assert app.post("/v1/events", json=first, headers=headers).status_code == 202
    assert fake.stored == {workspace_id: 1}, "a duplicate is not counted"

    fake.limit = 1
    second = {"events": [{"id": "evt_00000002", "subscription": "refund"}]}
    refused = app.post("/v1/events", json=second, headers=headers)
    assert refused.status_code == 402
    assert refused.json()["detail"] == "not stored: over the fake limit"


def test_an_extension_can_stop_a_workspaces_triggers(server) -> None:
    from rowfire.definitions import loads
    from rowfire.platform import scheduler
    from rowfire.platform.models import TriggerState

    workspace_id = _workspace()
    with platform_db.session_scope() as session:
        outcome = scheduler.poll(
            session,
            TriggerState(workspace_id=workspace_id, trigger_name="t"),
            loads("triggers: {}\n"),
            None,
            workspace_id=workspace_id,
        )
    assert outcome.skipped == "paused by the fake"


def test_extensions_need_the_hosted_locks(monkeypatch) -> None:
    monkeypatch.delenv(hosted.ENABLED_ENV, raising=False)
    extensions.use([Fake()])
    try:
        with pytest.raises(RuntimeError, match="ROWFIRE_HOSTED"):
            api.create_app(session=api.Session())
    finally:
        extensions.use(None)


def test_extensions_load_by_module_name(monkeypatch, tmp_path) -> None:
    (tmp_path / "rowfire_fake_ext.py").write_text(
        "from rowfire.extensions import Extension\n"
        "class Mine(Extension):\n    name = 'mine'\n"
        "def extension():\n    return Mine()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert [e.name for e in extensions._load("rowfire_fake_ext")] == ["mine"]
    assert extensions._load("") == []
    with pytest.raises(ModuleNotFoundError):
        extensions._load("rowfire_no_such_extension")


def test_public_egress_refuses_plain_http_and_private_addresses() -> None:
    with pytest.raises(dispatch.DispatchError):
        dispatch._check_url("http://hooks.example.com/x", dispatch.PUBLIC)
    dispatch._check_url("https://hooks.example.com/x", dispatch.PUBLIC)
    dispatch._check_url("inbox://demo/messages", dispatch.PUBLIC)
    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "localhost", "::1"):
        with pytest.raises(dispatch.DispatchError):
            dispatch.public_address(host, 443)


def test_with_no_extension_every_answer_is_rowfires_own() -> None:
    extensions.use([])
    try:
        workspace_id = uuid.uuid4()
        assert extensions.resolve({"host": "ext.test"}) is None
        assert extensions.may_store_events(None, workspace_id) is None
        assert extensions.may_poll(None, workspace_id) is None
        assert extensions.may_deliver(None, workspace_id) is None
        assert extensions.egress(None, workspace_id) is None
        assert extensions.health(None, workspace_id, "local") == {}
        assert extensions.routes() == []
    finally:
        extensions.use(None)
