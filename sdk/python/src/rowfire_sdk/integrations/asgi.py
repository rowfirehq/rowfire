"""ASGI middleware: each HTTP request becomes an operation named after its route.

    app.add_middleware(RowfireMiddleware)   # FastAPI / Starlette

The name is the route *template*, "POST /charges/{id}/refund", not the URL,
so every refund shares one name a subscription can match. Routing runs after
the middleware, so the name is resolved lazily, the first time something asks
for it (an ORM write, or the end of the request). FastAPI puts the matched
route in `scope["route"]`; plain Starlette only leaves the endpoint, so its
route is looked up from that.
"""

from __future__ import annotations

from collections.abc import Awaitable, MutableMapping
from typing import Any, Callable

import rowfire_sdk
from rowfire_sdk._context import Operation

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class RowfireMiddleware:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or rowfire_sdk.get_client() is None:
            await self.app(scope, receive, send)
            return

        op = Operation(
            resolve=lambda: route_name(scope),
            resolve_params=lambda: scope.get("path_params"),
        )

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                op.status = message["status"]
            await send(message)

        with rowfire_sdk.operation(op):
            await self.app(scope, receive, send_wrapper)


def route_name(scope: Scope) -> str | None:
    path = _route_path(scope)
    if path is None:
        return None
    return f"{scope['method']} {path}"


def _route_path(scope: Scope) -> str | None:
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    endpoint = scope.get("endpoint")
    app = scope.get("app")
    if endpoint is None or app is None:
        return None
    for candidate in getattr(app, "routes", ()):
        if getattr(candidate, "endpoint", None) is endpoint:
            return candidate.path
    return None
