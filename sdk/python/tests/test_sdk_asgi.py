"""A request is an operation named after its route template."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import rowfire_sdk as rowfire
from rowfire_sdk.integrations.asgi import RowfireMiddleware

UPGRADED = {
    "id": "plan_upgraded",
    "operation": "POST /accounts/{account_id}/upgrade",
    "where": {"params.account_id": "42"},
    "fields": ["params.account_id", "actor.id", "response.status", "operation"],
}


@pytest.fixture
def fastapi_app():
    app = FastAPI()
    app.add_middleware(RowfireMiddleware)

    @app.post("/accounts/{account_id}/upgrade")
    def upgrade(account_id: str):
        rowfire.set_actor(id="user-1")
        if account_id == "0":
            raise HTTPException(status_code=402)
        return {"ok": True}

    return app


def test_a_request_fires_under_its_route_template(start, ingest, fastapi_app):
    start([UPGRADED])
    with TestClient(fastapi_app) as http:
        assert http.post("/accounts/42/upgrade").status_code == 200
        assert http.post("/accounts/7/upgrade").status_code == 200
    assert rowfire.flush()
    [event] = ingest.events
    assert event["operation"] == "POST /accounts/{account_id}/upgrade"
    assert event["data"] == {
        "params.account_id": "42",
        "actor.id": "user-1",
        "response.status": 200,
        "operation": "POST /accounts/{account_id}/upgrade",
    }


def test_a_failed_request_does_not_fire(start, ingest, fastapi_app):
    start([{**UPGRADED, "where": {}}])
    with TestClient(fastapi_app) as http:
        assert http.post("/accounts/0/upgrade").status_code == 402
    assert rowfire.flush()
    assert ingest.events == []


def test_plain_starlette_routes_are_named_too(start, ingest):
    async def ping(request):
        return PlainTextResponse("pong")

    app = Starlette(routes=[Route("/ping/{who}", ping, methods=["GET"])])
    app.add_middleware(RowfireMiddleware)
    start([{"id": "ping", "operation": "GET /ping/{who}", "fields": ["params.who"]}])
    with TestClient(app) as http:
        http.get("/ping/me")
    assert rowfire.flush()
    assert ingest.events[0]["data"] == {"params.who": "me"}
