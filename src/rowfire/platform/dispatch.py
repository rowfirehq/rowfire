"""Turning a binding into an HTTP request, and making it.

Split in two on purpose:

    prepare()  -- pure. Renders templates into a request. No secrets, no I/O.
    send()     -- applies authentication and performs the call.

The split is what keeps a credential out of the database. `prepare` produces
exactly what gets written to `delivery.rendered`, which the UI displays;
`send` adds the token at the moment of the call and it is never stored.
Merging these would put an API key in a column and on a screen.

The whole of action execution is behind `send()`, which takes a prepared
request and returns an outcome. When delivery moves to an outbox queue, the
dispatcher calls the same function -- what changes is who calls it and when,
not this.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import templating
from .models import Action, AuthKind, Integration

USER_AGENT = "rowfire/0.1"

# The Demo inbox's scheme. A request to it is complete once it is recorded:
# `send` returns success without opening a connection, and the delivery row
# it leaves behind is what the Inbox page shows. It is a transport, not a
# provider check -- anything pointed at `inbox://` behaves this way.
INBOX_SCHEME = "inbox://"

# `inbox-only` refuses every URL but the Demo inbox's. Unset, the policy is
# the one described in _check_url.
EGRESS_ENV = "ROWFIRE_EGRESS"

# The policies a caller can ask for in place of the server's. `public` is for
# a signed-up account on a shared server (accounts.py): it may call real APIs,
# but only public https ones, and never the server's own network.
INBOX_ONLY = "inbox-only"
PUBLIC = "public"


def is_inbox(url: str | None) -> bool:
    return bool(url) and url.lower().startswith(INBOX_SCHEME)


class DispatchError(Exception):
    """A request that could not be built or sent. Never contains a secret."""


@dataclass(frozen=True)
class Prepared:
    """A request with no authentication applied. Safe to persist and display."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any] | None

    def as_record(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "body": self.body,
        }


@dataclass
class Outcome:
    status: int | None = None
    body: Any = None
    error: str | None = None
    # Whether another attempt could succeed. Nothing consumes this yet; the
    # dispatcher that will is the next increment.
    retryable: bool = False


def _policy(egress: str | None) -> str:
    return egress or os.environ.get(EGRESS_ENV, "").strip().lower()


def _check_url(url: str, egress: str | None = None) -> None:
    """Egress policy hook.

    By default permits everything, which is defensible while a deployment is
    single-tenant and self-hosted -- the operator already controls the
    network. A shared server asks for a policy instead: `inbox-only` for the
    public demo, `public` for signed-up accounts, whose addresses are checked
    again when the connection is made (see _public_request).
    """
    if is_inbox(url):
        return
    policy = _policy(egress)
    if policy == PUBLIC:
        if not url.lower().startswith("https://"):
            raise DispatchError(f"an action's URL must be https here, got {url[:40]!r}")
        return
    if policy == INBOX_ONLY:
        # A public instance (the hosted demo) must not be a way to make
        # requests to wherever a visitor likes: other sites, or the cloud's
        # own metadata endpoint. Only the in-process inbox is allowed.
        raise DispatchError(
            "outbound calls are switched off on this server; only the Demo inbox "
            "can be delivered to"
        )
    if not url.lower().startswith(("http://", "https://")):
        raise DispatchError(f"an action's URL must be http or https, got {url[:40]!r}")


def prepare(
    integration: Integration,
    action: Action,
    parameters: dict[str, Any],
    context: dict[str, Any],
) -> Prepared:
    """Render an action into a concrete request. No secrets involved."""
    try:
        # Binding parameters render first, then feed the action's own
        # templates -- so an action says `{{ channel }}` and the binding
        # decides where that comes from.
        resolved = templating.render(parameters, context)
        resolved = _coerce(resolved, action)
        merged = {**context, **_unsupplied(resolved, action, context), **resolved}

        path = (
            templating.render_string(action.path_template, merged) if action.path_template else ""
        )
        body = templating.render(action.body_template, merged) if action.body_template else None
        headers = {
            str(key): str(value)
            for key, value in templating.render(action.headers_template or {}, merged).items()
        }
    except templating.TemplateError as exc:
        raise DispatchError(str(exc)) from exc

    base = integration.base_url.rstrip("/")
    url = f"{base}{path}" if path else base

    headers = {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        **{str(k): str(v) for k, v in (integration.default_headers or {}).items()},
        **headers,
    }

    return Prepared(method=action.method.upper(), url=url, headers=headers, body=body)


def _unsupplied(values: dict[str, Any], action: Action, context: dict[str, Any]) -> dict[str, Any]:
    """Values for the optional parameters a binding left out.

    `bind` accepts a binding without them -- leaving one blank is the author's
    choice -- but the action's body still names them, so without this the
    request failed to render at send time. Each takes its declared default,
    or null. A name the fired row already supplies is left to the row, so
    declaring `event_time` still lets the automatic value through.
    """
    from .integrations import action_parameters

    out: dict[str, Any] = {}
    for name, spec in action_parameters(action).items():
        if name in values or spec.required:
            continue
        if spec.default is not None or name not in context:
            out[name] = spec.default
    return out


def _coerce(values: dict[str, Any], action: Action) -> dict[str, Any]:
    """Make each parameter the type its action declared.

    Row values arrive as strings -- a Decimal total becomes "12.50" on the way
    into the template context -- so an API that wants a JSON number would
    otherwise be sent a JSON string and reject it. The declared type is what
    makes that fixable; an inferred parameter has no type to coerce by.

    A value that cannot be converted is left exactly as it is rather than
    raising. The API's own error is more informative than a guess from here,
    and refusing to send is the wrong call for what may be a cosmetic mismatch.
    """
    from .integrations import ParameterType, action_parameters

    declared = action_parameters(action)
    if not declared:
        return values

    out = dict(values)
    for name, spec in declared.items():
        if name not in out or out[name] is None:
            continue
        out[name] = _as_type(out[name], spec.type, ParameterType)
    return out


def _as_type(value: Any, kind: Any, ParameterType: Any) -> Any:
    if kind is ParameterType.string:
        return value if isinstance(value, str) else json.dumps(value)
    if kind is ParameterType.number:
        if isinstance(value, bool) or not isinstance(value, str):
            return value
        try:
            return int(value) if value.strip().lstrip("-").isdigit() else float(value)
        except ValueError:
            return value
    if kind is ParameterType.boolean:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "t", "yes", "y", "1"}:
                return True
            if lowered in {"false", "f", "no", "n", "0"}:
                return False
        return value
    if kind is ParameterType.json and isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _authenticate(
    prepared: Prepared, integration: Integration, secret: str | None
) -> tuple[str, dict[str, str]]:
    """Apply auth at call time. The result is used and discarded."""
    headers = dict(prepared.headers)
    url = prepared.url

    if integration.auth_kind is AuthKind.none:
        return url, headers
    if secret is None:
        raise DispatchError(
            f"integration `{integration.name}` authenticates with "
            f"`{integration.auth_credential}`, but no such credential is stored"
        )

    if integration.auth_kind is AuthKind.bearer:
        headers["authorization"] = f"Bearer {secret}"
    elif integration.auth_kind is AuthKind.basic:
        # `secret` arrives as username:password, assembled by auth_secret so
        # that everything up to here handles one opaque string.
        headers["authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
    elif integration.auth_kind is AuthKind.header:
        if not integration.auth_header_name:
            raise DispatchError(
                f"integration `{integration.name}` uses header auth but names no header"
            )
        headers[integration.auth_header_name.lower()] = secret
    return url, headers


def send(
    prepared: Prepared,
    integration: Integration,
    action: Action,
    secret: str | None = None,
    *,
    transport: Any = None,
    egress: str | None = None,
) -> Outcome:
    """Perform the call, under `egress` if given, else the server's policy."""
    url, headers = _authenticate(prepared, integration, secret)
    _check_url(url, egress)

    if is_inbox(url):
        # Delivered by being recorded. Never handed to a transport, so the
        # inbox cannot reach the network whatever the caller passes in.
        return Outcome(status=200, body={"ok": True, "inbox": True})

    caller = transport or (_public_request if _policy(egress) == PUBLIC else http_request)
    try:
        status, payload = caller(
            prepared.method, url, headers, prepared.body, integration.timeout_ms
        )
    except Exception as exc:  # noqa: BLE001 -- one bad send must not kill a run
        return Outcome(error=_scrub(str(exc), secret), retryable=True)

    outcome = Outcome(status=status, body=payload)
    if status >= 400:
        outcome.error = f"{status}: {json.dumps(payload)[:300]}"
        outcome.retryable = status in set(action.retry_on or []) or status >= 500
        return outcome

    # Some APIs report failure in a 200 body. Slack is one: {"ok": false}.
    if isinstance(payload, dict) and payload.get("ok") is False:
        outcome.error = f"api reported failure: {json.dumps(payload)[:300]}"

    return outcome


def http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout_ms: int,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_ms / 1000) as response:
            raw = response.read().decode(errors="replace")
            return response.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace") if exc.fp else ""
        return exc.code, _maybe_json(raw)
    except urllib.error.URLError as exc:
        raise DispatchError(f"could not reach the integration: {exc.reason}") from exc


def public_address(host: str, port: int) -> str:
    """The address to connect to for `host`, if every one it has is public.

    Refused if any of them is loopback, private, link-local (the cloud's
    metadata endpoint among them) or otherwise not on the internet: a name
    that resolves to both a public and a private address is a way in, not a
    public service.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DispatchError(f"could not resolve {host}") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise DispatchError(f"could not resolve {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise DispatchError(f"{host} is not a public address; refusing to call it")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An https connection to one checked address, verified as `host`.

    The certificate is still checked against the host name; only the address
    is fixed. Resolving again at connect time would let a name answer with a
    public address for the check and a private one for the call.
    """

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def _public_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout_ms: int,
) -> tuple[int, Any]:
    """http_request for the `public` policy: pinned, https, no redirects.

    A redirect is returned as the response rather than followed, since
    following it would reach an address nobody checked.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    address = public_address(host, port)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    data = json.dumps(body).encode() if body is not None else None
    conn = _PinnedHTTPSConnection(host, address, port, timeout_ms / 1000)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode(errors="replace")
        return response.status, _maybe_json(raw)
    except (OSError, http.client.HTTPException) as exc:
        raise DispatchError(f"could not reach the integration: {type(exc).__name__}") from exc
    finally:
        conn.close()


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw[:500]


def _scrub(message: str, secret: str | None) -> str:
    """A secret must never reach an error column."""
    if secret and secret in message:
        message = message.replace(secret, "<redacted>")
    return message[:500]
