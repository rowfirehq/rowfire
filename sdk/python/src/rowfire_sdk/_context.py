"""The operation the current code is running inside.

A request or job sets it once, at its boundary, and everything below reads it:
the ORM hook tags each write with it. A `ContextVar` follows the code across
threads started with a copied context and across asyncio tasks, which is how
Sentry and OpenTelemetry carry their scope too.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

NO_OPERATION = "(none)"


class Operation:
    """One request, job or command.

    The name and the parameters can be resolved late: an ASGI middleware starts
    the operation before routing has run, so it passes functions that read the
    matched route once there is one.
    """

    __slots__ = ("_name", "_resolve", "_params", "_resolve_params", "actor", "status")

    def __init__(
        self,
        name: str | None = None,
        *,
        resolve: Callable[[], str | None] | None = None,
        actor: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        resolve_params: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._name = name
        self._resolve = resolve
        self._params = params
        self._resolve_params = resolve_params
        self.actor = actor
        self.status: int | None = None

    @property
    def params(self) -> dict[str, Any]:
        if self._params is None and self._resolve_params is not None:
            resolved = self._resolve_params()
            if resolved is not None:
                self._params = resolved
                return resolved
        return self._params or {}

    @params.setter
    def params(self, value: dict[str, Any] | None) -> None:
        self._params = value

    @property
    def name(self) -> str:
        if self._name is None and self._resolve is not None:
            resolved = self._resolve()
            if resolved:
                self._name = resolved
                return resolved
        return self._name or NO_OPERATION


current: ContextVar[Operation | None] = ContextVar("rowfire_operation", default=None)
