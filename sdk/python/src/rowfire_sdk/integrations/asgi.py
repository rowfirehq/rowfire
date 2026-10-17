"""ASGI middleware: each HTTP request becomes an operation named after its route.

    app.add_middleware(RowfireMiddleware)   # FastAPI / Starlette

The name is the route *template*, "POST /charges/{id}/refund", not the URL,
so every refund shares one name a subscription can match. Routing runs after
the middleware, so the name is resolved lazily, the first time something asks
for it (an ORM write, or the end of the request). FastAPI puts the matched
route in `scope["route"]`; plain Starlette only leaves the endpoint, so its
route is looked up from that.

On the first request it also lists every route the router knows to the
catalog, so the dashboard can offer operations before anyone has called them.
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
        self._declared = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        client = rowfire_sdk.get_client()
        if scope["type"] != "http" or client is None:
            await self.app(scope, receive, send)
            return
        if not self._declared:
            # The router knows every route, called or not: list them all once,
            # so the catalog shows operations nobody has used yet.
            self._declared = True
            client.declare_operations(declared_routes(scope.get("app")))

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


def declared_routes(app: Any) -> list[str]:
    names = []
    for route in getattr(app, "routes", ()):
        path = getattr(route, "path", None)
        for method in sorted(getattr(route, "methods", None) or ()):
            if path and method != "HEAD":
                names.append(f"{method} {path}")
    return names


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
