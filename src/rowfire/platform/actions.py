"""Delivery: what happens when a trigger fires.

One fired row runs every binding attached to its rule. Each binding names an
action on an integration, supplies its parameters, and produces one `delivery`
row. That is the whole model: an action per row, nothing chained, nothing
carried between them.

Shadow and live take the *same* path -- rendered identically, capped
identically, recorded identically. The only difference is whether the request
is actually sent. That is what makes promotion trustworthy: what runs live is
what you already watched run in shadow, not a different branch.

Execution sits behind `dispatch.send`, so moving delivery onto an outbox queue
changes who calls it and when, not how a request is built.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from . import dispatch, integrations, store
from .models import (
    DEFAULT_WORKSPACE_ID,
    Delivery,
    DeliveryStatus,
    Mode,
    Workspace,
)


def build_context(rule_name: str, row: dict[str, Any]) -> dict[str, Any]:
    """What templates can see.

    The fired row's columns are exposed at the top level for convenience, and
    again under `row` so a template can pass the whole thing.
    """
    plain = {key: _plain(value) for key, value in row.items()}
    return {
        **plain,
        "row": plain,
        "trigger": rule_name,
        "fired_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def deliver(
    session: Session,
    *,
    workspace: Workspace,
    rule_name: str,
    mode: Mode,
    row: dict[str, Any],
    fire_id: int | None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
    master_key: bytes | None = None,
    transport: Any = None,
) -> list[Delivery]:
    """Run every binding for this trigger. Returns one Delivery each."""
    bound = integrations.bindings_for(session, rule_name, workspace_id=workspace_id)
    if not bound:
        return []

    context = build_context(rule_name, row)
    results: list[Delivery] = []

    for binding, action, integration in bound:
        delivery = _deliver_one(
            session,
            workspace=workspace,
            rule_name=rule_name,
            mode=mode,
            fire_id=fire_id,
            binding=binding,
            action=action,
            integration=integration,
            context=context,
            workspace_id=workspace_id,
            master_key=master_key,
            transport=transport,
        )
        results.append(delivery)

    return results


def _deliver_one(
    session: Session,
    *,
    workspace: Workspace,
    rule_name: str,
    mode: Mode,
    fire_id: int | None,
    binding: Any,
    action: Any,
    integration: Any,
    context: dict[str, Any],
    workspace_id: uuid.UUID,
    master_key: bytes | None,
    transport: Any,
) -> Delivery:
    delivery = Delivery(
        workspace_id=workspace_id,
        fire_id=fire_id,
        rule_name=rule_name,
        channel=integration.name,
        mode=mode,
        action_id=action.id,
        status=DeliveryStatus.pending,
    )

    try:
        prepared = dispatch.prepare(integration, action, binding.parameters, context)
    except dispatch.DispatchError as exc:
        delivery.status = DeliveryStatus.failed
        delivery.error = str(exc)
        session.add(delivery)
        session.flush()
        return delivery

    # Never contains authentication -- see dispatch.prepare.
    delivery.rendered = prepared.as_record()
    delivery.request_url = prepared.url

    recipient = _recipient(binding, context, integration.name, action.name)
    delivery.recipient = recipient

    # Budgets are scoped by mode: shadow walks the same path but must not
    # spend the real allowance, or promotion arrives to find it exhausted.
    allowed, count = store.consume_budget(
        session,
        f"{mode.value}:{recipient}",
        cap=workspace.cap_per_recipient,
        window_hours=workspace.cap_window_hours,
        workspace_id=workspace_id,
    )
    if not allowed:
        delivery.status = DeliveryStatus.suppressed
        delivery.suppressed_reason = (
            f"frequency cap: {count} of {workspace.cap_per_recipient} "
            f"in {workspace.cap_window_hours}h"
        )
        session.add(delivery)
        session.flush()
        return delivery

    if mode is Mode.shadow:
        # Fully rendered and recorded, deliberately not sent.
        delivery.status = DeliveryStatus.suppressed
        delivery.suppressed_reason = "shadow mode — rendered and recorded, not sent"
        session.add(delivery)
        session.flush()
        return delivery

    try:
        secret = integrations.auth_secret(integration, master_key=master_key)
    except Exception as exc:  # noqa: BLE001 -- e.g. a rotated master key
        delivery.status = DeliveryStatus.failed
        delivery.error = f"could not read the integration credential: {type(exc).__name__}"
        session.add(delivery)
        session.flush()
        return delivery

    delivery.attempts = 1
    outcome = dispatch.send(prepared, integration, action, secret, transport=transport)
    delivery.response_status = outcome.status

    if outcome.error:
        delivery.status = DeliveryStatus.failed
        delivery.error = outcome.error
    else:
        delivery.status = DeliveryStatus.sent
        delivery.sent_at = datetime.now(UTC)

    session.add(delivery)
    session.flush()
    return delivery


def _recipient(binding: Any, context: dict[str, Any], integration: str, action: str) -> str:
    """What the frequency cap counts against.

    Defaults to the integration and action, so a channel is capped as a whole.
    A binding can narrow it -- `{{ phone }}` for a per-person cap.
    """
    from . import templating

    if binding.recipient_template:
        try:
            return str(templating.render_string(binding.recipient_template, context))
        except templating.TemplateError:
            pass
    return f"{integration}:{action}"


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ", timespec="minutes")
    return str(value)
