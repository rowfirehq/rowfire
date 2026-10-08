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
import json
import os
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


def _check_url(url: str) -> None:
    """Egress policy hook.

    Currently permits everything, which is defensible while every deployment
    is single-tenant and self-hosted -- the operator already controls the
    network. It is a named function rather than an absent one so that adding a
    policy later (refusing loopback, RFC1918 and 169.254.0.0/16, re-checked
    after redirects) is a change here and nowhere else.
    """
    if is_inbox(url):
        return
    if os.environ.get(EGRESS_ENV, "").strip().lower() == "inbox-only":
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
) -> Outcome:
    """Perform the call."""
    url, headers = _authenticate(prepared, integration, secret)
    _check_url(url)

    if is_inbox(url):
        # Delivered by being recorded. Never handed to a transport, so the
        # inbox cannot reach the network whatever the caller passes in.
        return Outcome(status=200, body={"ok": True, "inbox": True})

    caller = transport or http_request
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
