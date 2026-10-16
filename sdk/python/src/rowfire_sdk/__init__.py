"""Rowfire's in-app SDK (prototype).

Installed once, it tags each database write with the operation that caused it
(a route, a job) and sends Rowfire only the writes and operations that a
subscription asks for, with only the fields it names. Subscriptions are
fetched from Rowfire at runtime, so a new event needs no deploy; the ceiling,
set here in code, bounds what any subscription can ask for.

    import rowfire_sdk as rowfire
    from rowfire_sdk.integrations import sqlalchemy as rowfire_sqlalchemy

    rowfire.init(
        endpoint="https://rowfire.internal",
        api_key=os.environ["ROWFIRE_API_KEY"],
        ceiling=rowfire.Ceiling(deny_fields=["*email*", "*password*", "*token*"]),
        spool_dir="/var/lib/myapp/rowfire",
    )
    rowfire_sqlalchemy.instrument()

    with rowfire.operation("job AutoRefundWorker", actor={"role": "system"}):
        ...

Nothing here raises into the app or waits on the network: if Rowfire is down
the app carries on, and what could not be sent is retried or counted as lost.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rowfire_sdk._client import Change, Client
from rowfire_sdk._context import Operation, current
from rowfire_sdk._matching import Ceiling
from rowfire_sdk._version import VERSION

__all__ = [
    "VERSION",
    "Ceiling",
    "Change",
    "Client",
    "Operation",
    "close",
    "current_operation",
    "flush",
    "get_client",
    "init",
    "operation",
    "set_actor",
    "stats",
]

log = logging.getLogger("rowfire_sdk")

_client: Client | None = None
_atexit_registered = False


def init(endpoint: str, api_key: str, **options: Any) -> Client:
    """Start the SDK. Options are those of `Client`."""
    global _client, _atexit_registered
    if _client is not None:
        _client.close(timeout=0.5)
    _client = Client(endpoint, api_key, **options)
    if not _atexit_registered:
        atexit.register(_shutdown)
        _atexit_registered = True
    return _client


def get_client() -> Client | None:
    return _client


def current_operation() -> Operation | None:
    return current.get()


@contextmanager
def operation(
    name: str | Operation,
    *,
    actor: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Iterator[Operation]:
    """Run the block as one operation; also usable as a decorator.

    Subscriptions without an `on` table fire when it ends without an exception
    (and, for a request, with a status below 400).
    """
    op = name if isinstance(name, Operation) else Operation(name, actor=actor, params=params)
    token = current.set(op)
    try:
        yield op
    except BaseException:
        raise
    else:
        client = _client
        if client is not None:
            try:
                client.operation_finished(op)
            except Exception:
                log.exception("rowfire-sdk: could not match the finished operation")
    finally:
        current.reset(token)


def set_actor(**actor: Any) -> None:
    """Say who is acting in the current operation, typically from auth middleware."""
    op = current.get()
    if op is not None:
        op.actor = {**(op.actor or {}), **actor}


def flush(timeout: float = 2.0) -> bool:
    return _client.flush(timeout) if _client is not None else True


def close(timeout: float = 2.0) -> None:
    global _client
    if _client is not None:
        _client.close(timeout)
        _client = None


def stats() -> dict[str, Any]:
    return _client.stats() if _client is not None else {}


def _shutdown() -> None:
    close()
