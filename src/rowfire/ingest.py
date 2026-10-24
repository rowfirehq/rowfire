"""The ingest server: where apps running the in-app SDK send their events.

    rowfire ingest --host 0.0.0.0 --port 8100

Its own server by default. The UI's API is a single-user tool that refuses
any Host header but loopback, so a page the user browses cannot drive it
(api.py). This one has to be reached by the app's servers over the network,
so it is authenticated instead: every request carries an API key
(`rowfire platform ingest-key create`), which names the workspace it writes
to. It serves nothing else -- no UI, no definitions, no credentials -- so
opening it to the network opens only this.

A deploy that should not run a second service can have the UI's server carry
these routes as well (ROWFIRE_SERVE_INGEST=1, which `rowfire cloud` sets).
They are exempt from its loopback check and only from that: a page in the
user's browser has no API key to send, which is what the check exists to
stop.

    POST /v1/events    a batch of events, stored once each (see events.py)
    POST /v1/catalog   one app process's catalog snapshot
    GET  /v1/config    the subscriptions to match, with an ETag
    GET  /healthz      whether the control plane answers

Answers follow what the SDK's transport does with them: 2xx is delivered,
408/429/5xx is retried later (503 with Retry-After when the database is down),
any other 4xx is dropped and counted as lost, so it is kept for requests that
would fail again identically. Put TLS in front of it: keys travel in a header.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from .platform import db as platform_db
from .platform import events as event_store
from .platform import store

# What one request may carry, before and after gzip. The SDK sends batches of
# at most a few hundred events, a few hundred KB of JSON.
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_DECODED_BYTES = 20 * 1024 * 1024
MAX_EVENTS_PER_BATCH = 1_000
RETRY_AFTER_SECONDS = 5

T = TypeVar("T")


class EventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    subscription: str = Field(min_length=1, max_length=200)
    operation: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    config_version: str | None = Field(default=None, max_length=100)
    data: dict[str, Any] = Field(default_factory=dict)


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[EventIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)
    lost: dict[str, int] = Field(default_factory=dict)


class _Refused(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.detail = detail
        self.headers = headers or {}


PATH_PREFIX = "/v1/"


def create_ingest_app() -> FastAPI:
    """The ingest server on its own: the /v1 routes and a health check."""
    app = FastAPI(title="Rowfire ingest", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        try:
            with platform_db.session_scope() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 -- reported, not raised
            raise _Refused(503, f"control plane unavailable: {type(exc).__name__}") from exc
        return {"ok": True}

    install(app)
    return app


def install(app: FastAPI) -> None:
    """Add the /v1 routes to `app`.

    Used by the ingest server above, and by the UI's server when it is asked
    to carry them too (ROWFIRE_SERVE_INGEST, see api.py) -- so a deploy that
    already pays for one web service needs no second one.
    """

    @app.exception_handler(_Refused)
    async def refused(request: Request, exc: _Refused) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=exc.headers)

    @app.post("/v1/events", status_code=202)
    async def receive_events(request: Request) -> dict[str, Any]:
        raw = await _read_json(request)
        try:
            batch = BatchIn.model_validate(raw)
        except ValidationError as exc:
            raise _Refused(400, _first_error(exc)) from exc
        events = [event.model_dump() for event in batch.events]
        key = _bearer(request)

        def work(session: Any) -> dict[str, Any]:
            workspace_id = _workspace(session, key)
            stored, duplicates = event_store.store_events(
                session, events, workspace_id=workspace_id
            )
            event_store.record_losses(session, batch.lost, workspace_id=workspace_id)
            return {"stored": stored, "duplicates": duplicates}

        return await run_in_threadpool(_in_session, work)

    @app.post("/v1/catalog", status_code=202)
    async def receive_catalog(request: Request) -> dict[str, Any]:
        raw = await _read_json(request)
        if not isinstance(raw, dict) or not isinstance(raw.get("instance"), dict):
            raise _Refused(400, "a catalog needs an `instance` object")
        key = _bearer(request)

        def work(session: Any) -> dict[str, Any]:
            workspace_id = _workspace(session, key)
            try:
                event_store.save_catalog(session, raw, workspace_id=workspace_id)
            except event_store.EventsError as exc:
                raise _Refused(400, str(exc)) from exc
            return {"stored": True}

        return await run_in_threadpool(_in_session, work)

    @app.get("/v1/config")
    async def config(request: Request) -> Response:
        key = _bearer(request)

        def work(session: Any) -> tuple[dict[str, Any], str]:
            workspace_id = _workspace(session, key)
            active = store.active_definitions(session, workspace_id=workspace_id)
            if active is None:
                return event_store.sdk_config(None, None)
            version, definitions = active
            return event_store.sdk_config(definitions, version.version)

        body, etag = await run_in_threadpool(_in_session, work)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(body, headers={"ETag": etag})


# --------------------------------------------------------------- helpers


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _Refused(401, "send an API key: Authorization: Bearer rf_…", _CHALLENGE)
    return token.strip()


_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="rowfire"'}


def _workspace(session: Any, presented: str) -> Any:
    key = event_store.authenticate(session, presented)
    if key is None:
        raise _Refused(401, "unknown or revoked API key", _CHALLENGE)
    return key.workspace_id


def _in_session(work: Callable[[Any], T]) -> T:
    """Run `work` in a control-plane transaction.

    A refusal (bad key, bad body) passes through as itself. Anything else --
    the database down, a deadlock -- becomes a 503 the SDK retries, since
    the same request may well succeed in a moment.
    """
    try:
        with platform_db.session_scope() as session:
            return work(session)
    except _Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


def _unavailable(exc: BaseException) -> _Refused:
    return _Refused(
        503,
        f"could not store this right now ({type(exc).__name__}); retry",
        {"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


async def _read_json(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _Refused(413, f"body over {MAX_BODY_BYTES} bytes")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise _Refused(413, f"body over {MAX_BODY_BYTES} bytes")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        # Bounded, so a small body cannot expand into gigabytes.
        inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        try:
            body = inflater.decompress(body, MAX_DECODED_BYTES + 1)
        except zlib.error as exc:
            raise _Refused(400, "body is not valid gzip") from exc
        if len(body) > MAX_DECODED_BYTES or inflater.unconsumed_tail:
            raise _Refused(413, f"body over {MAX_DECODED_BYTES} bytes once decompressed")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise _Refused(400, "body is not valid JSON") from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}"
