"""Extensions: code outside this repository that plugs into a running Rowfire.

    ROWFIRE_EXTENSIONS=some_package,another

Each name is a module importable on this server with an `extension()`
function returning an `Extension`. It is loaded once per process, by the web
server, the ingest server and the worker alike, and asked at a few fixed
points what it wants to do:

  * whose request this is, for a host name it serves (`resolve`): its own
    sign-in, its own cookie, the workspace that request acts on
  * extra API routes (`routes`)
  * what to add to /api/health, for the UI to show (`health`). One key the
    UI draws by itself: `banner`, a line across the top of every page --

        {"text": "Trial: 9 days left",
         "actions": [{"label": "Upgrade", "href": "/upgrade"},
                     {"label": "Sign out", "post": "/api/x/logout",
                      "then": "/signin"}]}
  * whether a workspace may store events, run its triggers or send a live
    request (`may_store_events`, `may_poll`, `may_deliver`), and which
    egress policy its requests go out under (`egress`, see dispatch.py)
  * that events were stored (`events_stored`), to count them

With none configured, every answer is the one Rowfire gives on its own, so
a local install and the public demo never notice this module exists.

The questions are the contract. Changing what one of them means is a
breaking change for every extension, which tests/test_extensions.py exists
to catch.
"""

from __future__ import annotations

import importlib
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ENV = "ROWFIRE_EXTENSIONS"


@dataclass(frozen=True)
class Resolution:
    """Whose request this is, as an extension decided it.

    `workspace_id` is None for someone the extension has not signed in:
    they may reach `open_paths` and nothing else, and every other request is
    refused with `refusal` as its 401 body. `signin_url` is where the UI
    sends them, a page the extension serves itself.
    """

    realm: str
    workspace_id: uuid.UUID | None
    open_paths: frozenset[str] = frozenset()
    refusal: dict[str, Any] = field(default_factory=lambda: {"detail": "Sign in to continue."})
    signin_url: str | None = None


class Extension:
    """What an extension can do. Every method's default changes nothing."""

    name = "extension"

    def routes(self) -> list[Any]:
        """FastAPI routers to add to the web server."""
        return []

    def resolve(self, headers: dict[str, str]) -> Resolution | None:
        """Claim a request (headers lower-cased), or None to leave it be.

        Runs in a worker thread, so it may read the database.
        """
        return None

    def health(self, session: Any, workspace_id: uuid.UUID, realm: str) -> dict[str, Any]:
        """Fields to add to /api/health for a request in `realm`."""
        return {}

    def may_store_events(self, session: Any, workspace_id: uuid.UUID) -> str | None:
        """None if the workspace may store more events, else why not."""
        return None

    def may_poll(self, session: Any, workspace_id: uuid.UUID) -> str | None:
        """None if the workspace's triggers may run, else why not."""
        return None

    def may_deliver(self, session: Any, workspace_id: uuid.UUID) -> str | None:
        """None if a live request may be sent for the workspace, else why not."""
        return None

    def egress(self, session: Any, workspace_id: uuid.UUID) -> str | None:
        """The egress policy for the workspace's requests, or None for the server's."""
        return None

    def events_stored(self, session: Any, workspace_id: uuid.UUID, count: int) -> None:
        """`count` new events were stored for the workspace."""


_lock = threading.Lock()
_loaded: list[Extension] | None = None


def active() -> list[Extension]:
    """The extensions this process runs with, loaded on first use."""
    global _loaded
    if _loaded is None:
        with _lock:
            if _loaded is None:
                _loaded = _load(os.environ.get(ENV, ""))
    return _loaded


def _load(spec: str) -> list[Extension]:
    loaded: list[Extension] = []
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        # A missing or broken extension stops the process rather than being
        # skipped: running without the code that signs people in or enforces
        # their limits would serve every request as if it had none.
        module = importlib.import_module(name)
        extension = module.extension()
        if not isinstance(extension, Extension):
            raise TypeError(f"{name}.extension() did not return a rowfire Extension")
        loaded.append(extension)
    return loaded


def use(extensions: list[Extension] | None) -> None:
    """Replace the loaded set, or with None reload it from the environment."""
    global _loaded
    with _lock:
        _loaded = extensions


def resolve(headers: dict[str, str]) -> Resolution | None:
    for extension in active():
        resolution = extension.resolve(headers)
        if resolution is not None:
            return resolution
    return None


def _first(ask: Callable[[Extension], str | None]) -> str | None:
    for extension in active():
        answer = ask(extension)
        if answer is not None:
            return answer
    return None


def may_store_events(session: Any, workspace_id: uuid.UUID) -> str | None:
    return _first(lambda e: e.may_store_events(session, workspace_id))


def may_poll(session: Any, workspace_id: uuid.UUID) -> str | None:
    return _first(lambda e: e.may_poll(session, workspace_id))


def may_deliver(session: Any, workspace_id: uuid.UUID) -> str | None:
    return _first(lambda e: e.may_deliver(session, workspace_id))


def egress(session: Any, workspace_id: uuid.UUID) -> str | None:
    return _first(lambda e: e.egress(session, workspace_id))


def events_stored(session: Any, workspace_id: uuid.UUID, count: int) -> None:
    if count <= 0:
        return
    for extension in active():
        extension.events_stored(session, workspace_id, count)


def health(session: Any, workspace_id: uuid.UUID, realm: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for extension in active():
        out.update(extension.health(session, workspace_id, realm))
    return out


def routes() -> list[Any]:
    return [router for extension in active() for router in extension.routes()]
