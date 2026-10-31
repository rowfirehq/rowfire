"""Which workspace the current request acts on.

Every table in the control plane already carries `workspace_id`, and the
store, scheduler and ledger take it as a parameter. What decides it for a
request is here, in one place, so the API passes `current()` everywhere and
never reaches for the default on its own.

On a local install there is one workspace and this is always it. A hosted
demo binds each visitor's own workspace for the length of the request (see
`hosted`), and a request that reached for the default instead would read
somebody else's rules -- which is why nothing in the API names the default.

A context variable rather than a parameter on every endpoint: it is set by
the ASGI middleware before the endpoint runs and is copied into the worker
thread a sync endpoint runs in, so every call below sees the same value.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .platform.models import DEFAULT_WORKSPACE_ID

_current: ContextVar[uuid.UUID] = ContextVar("rowfire_workspace", default=DEFAULT_WORKSPACE_ID)


def current() -> uuid.UUID:
    """The workspace this request acts on."""
    return _current.get()


@contextmanager
def acting_as(workspace_id: uuid.UUID) -> Iterator[None]:
    """Act on `workspace_id` until the block ends."""
    token = _current.set(workspace_id)
    try:
        yield
    finally:
        _current.reset(token)


# What kind of request this is. `local` on a local install; on a public
# instance, `demo` for a demo visitor, or the name an extension gave the
# requests it claimed (see extensions.Resolution) -- the same server can
# answer more than one, told apart by the host name a request came in on.
LOCAL = "local"
DEMO = "demo"

_realm: ContextVar[str] = ContextVar("rowfire_realm", default=LOCAL)


def realm() -> str:
    return _realm.get()


@contextmanager
def in_realm(name: str) -> Iterator[None]:
    token = _realm.set(name)
    try:
        yield
    finally:
        _realm.reset(token)
