"""Integrations and actions: the catalogue, installing, editing, binding.

Two entities, and the split matters.

An **integration** is a named, configured connection to an outside system --
"Acme Slack", "Acme Braze". It holds the base URL, how to
authenticate, and the credentials themselves (encrypted). It is an instance:
two Slack workspaces are two integrations.

An **action** is one REST call on an integration -- send a message, update a
custom field. It is described entirely as data: method, path, headers and
body, with `{{ placeholders }}` for whatever the caller supplies.

Both are rows. The YAML in `catalogue/` exists only to prefill the form for
systems we happen to know about; it is loaded through the same validation and
produces the same rows as something typed into the UI. Nothing in the engine
branches on which system an integration points at -- the moment it did, the
claim that anyone can add their own would stop being true.
"""

from __future__ import annotations

import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from . import crypto, templating
from .models import (
    DEFAULT_WORKSPACE_ID,
    Action,
    AuthKind,
    Integration,
    RuleBinding,
)

CATALOGUE_DIR = Path(__file__).parent / "catalogue"

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Names the dispatcher always puts in the context, so a binding need not
# supply them.
ROW_CONTEXT = {"trigger", "row", "entity_id", "event_time", "fired_at"}


class IntegrationError(Exception):
    """Raised for malformed templates and bad bindings. Never carries a secret."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


# ------------------------------------------------------------- catalogue


class CredentialSpec(BaseModel):
    """One value an integration needs before it can authenticate."""

    model_config = ConfigDict(extra="forbid")

    label: str
    help: str | None = None
    # False for things that are configuration rather than secrets -- an
    # account id, a region. Only secrets are write-only in the UI.
    secret: bool = True
    required: bool = True


class AuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: AuthKind = AuthKind.none
    header_name: str | None = None
    # Which credential carries the secret. Named rather than assumed, so an
    # integration can hold several values and still say which one signs.
    credential: str = "token"
    # For basic auth: which credential is the username half.
    username_credential: str | None = None


class ParameterType(StrEnum):
    """What a parameter is, so it can be asked for and sent correctly."""

    string = "string"
    number = "number"
    boolean = "boolean"
    # An object or array passed through whole, for APIs that take nested
    # structures -- Braze's event `properties`, for instance.
    json = "json"


class ParameterSpec(BaseModel):
    """One value an action needs, declared rather than inferred.

    Inferring parameters from the placeholders in a body recovers their names
    and nothing else: no type, no label, no idea whether one is optional. That
    is enough to render a row of unlabelled text boxes and not much more, and
    it cannot tell a typo'd placeholder from a new input.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    type: ParameterType = ParameterType.string
    help: str | None = None
    required: bool = True
    default: Any = None

    def display(self, name: str) -> str:
        return self.label or name.replace("_", " ")


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    method: str = "POST"
    path: str = ""
    # The contract: what whoever attaches this action has to supply.
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    retry_on: list[int] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        if value.upper() not in ALLOWED_METHODS:
            raise ValueError(f"method must be one of {sorted(ALLOWED_METHODS)}")
        return value.upper()

    def used(self) -> set[str]:
        """Placeholders the request templates reference.

        Row-context names are *not* stripped here, because an action is
        allowed to declare one. Braze wants an `event_time` on the event it
        records, which collides with the name the dispatcher fills from the
        fired row -- and the right answer is that declaring it lets a binding
        override, while leaving it undeclared keeps the automatic value.
        """
        names = (
            templating.placeholders(self.body)
            | templating.placeholders(self.headers)
            | templating.placeholders(self.path)
        )
        return {n for n in names if "." not in n}

    def inputs(self) -> set[str]:
        """What a binding must supply.

        The declaration when there is one, and otherwise the placeholders --
        so an action written before parameters existed, or edited quickly
        without them, still works and still validates.
        """
        if self.parameters:
            return set(self.parameters)
        return self.used() - ROW_CONTEXT

    def check(self, label: str) -> list[str]:
        """Where the declaration and the templates disagree.

        Both directions are worth catching. A placeholder nothing declares is
        usually a typo, and used to become a phantom parameter. A declared
        parameter nothing uses is a field the UI will ask for and then throw
        away.
        """
        if not self.parameters:
            return []
        errors: list[str] = []
        declared, used = set(self.parameters), self.used()
        # A row-context name may be used without being declared: the
        # dispatcher supplies it either way.
        for name in sorted(used - declared - ROW_CONTEXT):
            errors.append(
                f"{label}: the request uses `{{{{ {name} }}}}`, which is not a "
                f"declared parameter. Declared: {', '.join(sorted(declared)) or 'none'}"
            )
        for name in sorted(declared - used):
            errors.append(
                f"{label}: parameter `{name}` is declared but never used in the "
                f"path, headers or body"
            )
        return errors


class IntegrationTemplate(BaseModel):
    """A catalogue entry: what we know about a system before it is configured."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    base_url: str = ""
    auth: AuthSpec = Field(default_factory=AuthSpec)
    credentials: dict[str, CredentialSpec] = Field(default_factory=dict)
    timeout_ms: int = 10_000
    actions: dict[str, ActionSpec] = Field(default_factory=dict)

    def inputs(self, action_name: str) -> set[str]:
        return self.actions[action_name].inputs()


def available() -> list[str]:
    return sorted(path.stem for path in CATALOGUE_DIR.glob("*.yaml"))


def load_template(name: str) -> IntegrationTemplate:
    """Load a catalogue entry by name."""
    path = CATALOGUE_DIR / f"{name}.yaml"
    if not path.exists():
        raise IntegrationError(
            [f"unknown integration template `{name}`. Available: {', '.join(available())}"]
        )
    return parse_template(path.read_text(), label=name)


def parse_template(text: str, label: str = "integration") -> IntegrationTemplate:
    """Parse and validate a template, bundled or hand-written."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise IntegrationError([f"{label} is not valid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise IntegrationError([f"{label} must be a YAML mapping"])

    try:
        template = IntegrationTemplate.model_validate(raw)
    except Exception as exc:
        raise IntegrationError([f"{label}: {exc}"]) from exc

    _check_auth(template.auth, template.credentials, label, errors_out=None)

    errors: list[str] = []
    for action_name, spec in template.actions.items():
        errors.extend(spec.check(f"{label}.{action_name}"))
    if errors:
        raise IntegrationError(errors)
    return template


def _check_auth(
    auth: AuthSpec,
    credentials: dict[str, CredentialSpec],
    label: str,
    errors_out: list[str] | None,
) -> None:
    """An integration that cannot authenticate is a configuration error."""
    errors: list[str] = []
    if auth.kind is AuthKind.header and not auth.header_name:
        errors.append(f"{label}: header auth requires auth.header_name")
    if auth.kind is AuthKind.basic:
        if not auth.username_credential:
            errors.append(f"{label}: basic auth requires auth.username_credential")
        elif credentials and auth.username_credential not in credentials:
            errors.append(
                f"{label}: basic auth names `{auth.username_credential}` as the "
                f"username, which is not a declared credential"
            )
    if auth.kind is not AuthKind.none and credentials and auth.credential not in credentials:
        errors.append(
            f"{label}: auth uses credential `{auth.credential}`, which is not "
            f"declared. Declared: {', '.join(sorted(credentials)) or 'none'}"
        )
    if errors:
        if errors_out is None:
            raise IntegrationError(errors)
        errors_out.extend(errors)


# ------------------------------------------------------------ credentials


def seal_credentials(
    integration: Integration,
    values: dict[str, str],
    master_key: bytes | None = None,
) -> None:
    """Encrypt a credential map onto an integration, in place.

    The same envelope as a database credential: a per-row data key wrapped by
    the master key, so adopting a KMS later rewraps rather than re-encrypting.
    """
    envelope = crypto.encrypt(json.dumps(values), master_key=master_key)
    integration.credentials_ciphertext = envelope.ciphertext
    integration.credentials_nonce = envelope.nonce
    integration.wrapped_data_key = envelope.wrapped_data_key
    integration.wrap_nonce = envelope.wrap_nonce
    integration.key_id = envelope.key_id
    integration.algorithm = envelope.algorithm


def reveal_credentials(integration: Integration, master_key: bytes | None = None) -> dict[str, str]:
    """Decrypt an integration's credentials. Must not be logged or stored."""
    if integration.credentials_ciphertext is None:
        return {}
    envelope = crypto.Envelope(
        ciphertext=integration.credentials_ciphertext,
        nonce=integration.credentials_nonce or b"",
        wrapped_data_key=integration.wrapped_data_key or b"",
        wrap_nonce=integration.wrap_nonce or b"",
        key_id=integration.key_id or crypto.LOCAL_KEY_ID,
        algorithm=integration.algorithm or crypto.ALGORITHM,
    )
    plaintext = crypto.decrypt(envelope, master_key=master_key)
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError:
        # A credential sealed before this column held a map: one bare secret.
        # Read it as the one the auth config names, so an upgrade needs no
        # re-encryption and no downtime.
        return {integration.auth_credential: plaintext}
    if not isinstance(decoded, dict):
        return {integration.auth_credential: str(decoded)}
    return {str(k): str(v) for k, v in decoded.items()}


def auth_secret(integration: Integration, master_key: bytes | None = None) -> str | None:
    """The one credential that signs the request, or None when auth is off."""
    if integration.auth_kind is AuthKind.none:
        return None
    values = reveal_credentials(integration, master_key=master_key)
    secret = values.get(integration.auth_credential)
    if integration.auth_kind is AuthKind.basic:
        # Assembled here as username:password, so the dispatcher handles one
        # opaque string whatever the auth kind. Either half missing means
        # there is nothing to sign with.
        username = values.get(integration.auth_username_credential or "")
        if secret is None or username is None:
            return None
        return f"{username}:{secret}"
    return secret


# --------------------------------------------------------------- install


def install(
    session: Session,
    template: IntegrationTemplate,
    *,
    name: str | None = None,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    description: str | None = None,
    master_key: bytes | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> Integration:
    """Create or update an integration from a catalogue template.

    Idempotent on the name, so re-running a seed updates rather than
    duplicates. Actions in the template are upserted; actions added by hand
    afterwards are left alone, because the catalogue does not own them.
    """
    integration_name = name or template.name
    values = dict(credentials or {})

    missing = [
        key for key, spec in template.credentials.items() if spec.required and not values.get(key)
    ]
    if missing and template.auth.kind is not AuthKind.none:
        labels = ", ".join(f"{k} ({template.credentials[k].label})" for k in missing)
        raise IntegrationError([f"`{integration_name}` needs {labels}"])

    existing = session.exec(
        select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.name == integration_name,
        )
    ).first()

    integration = existing or Integration(
        workspace_id=workspace_id, name=integration_name, base_url=""
    )
    integration.provider = template.name
    integration.description = description or template.description
    integration.base_url = base_url if base_url is not None else template.base_url
    integration.auth_kind = template.auth.kind
    integration.auth_header_name = template.auth.header_name
    integration.auth_credential = template.auth.credential
    integration.auth_username_credential = template.auth.username_credential
    integration.timeout_ms = template.timeout_ms
    integration.updated_at = _now()

    if values:
        seal_credentials(integration, values, master_key=master_key)

    session.add(integration)
    session.flush()

    known = {
        action.name: action
        for action in session.exec(
            select(Action).where(Action.integration_id == integration.id)
        ).all()
    }
    for action_name, action_spec in template.actions.items():
        action = known.get(action_name) or Action(integration_id=integration.id, name=action_name)
        _apply_action_spec(action, action_spec)
        session.add(action)

    session.flush()
    return integration


def _apply_action_spec(action: Action, spec: ActionSpec) -> None:
    action.description = spec.description
    action.method = spec.method
    action.path_template = spec.path
    action.parameters = {
        name: parameter.model_dump(mode="json") for name, parameter in spec.parameters.items()
    }
    action.body_template = spec.body
    action.headers_template = spec.headers
    action.retry_on = spec.retry_on
    action.updated_at = _now()


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


# ------------------------------------------------------- hand-made actions


def upsert_action(
    session: Session,
    integration: Integration,
    name: str,
    spec: ActionSpec,
) -> Action:
    """Add or replace one action on an integration.

    This is the path the UI uses, and it is the same validation and the same
    row the catalogue produces. An action nobody can describe without writing
    Python is the thing this design exists to avoid.
    """
    if not name:
        raise IntegrationError(["an action needs a name"])

    problems = spec.check(f"action `{name}`")
    if problems:
        raise IntegrationError(problems)

    action = session.exec(
        select(Action).where(Action.integration_id == integration.id, Action.name == name)
    ).first() or Action(integration_id=integration.id, name=name)

    _apply_action_spec(action, spec)
    session.add(action)
    session.flush()
    return action


def delete_action(session: Session, action: Action) -> None:
    session.delete(action)
    session.flush()


# ---------------------------------------------------------------- binding


def bind(
    session: Session,
    rule_name: str,
    integration_name: str,
    action_name: str,
    parameters: dict[str, Any],
    *,
    recipient_template: str | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> RuleBinding:
    """Attach an action to a rule, checking the parameters up front."""
    integration = session.exec(
        select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.name == integration_name,
        )
    ).first()
    if integration is None:
        raise IntegrationError([f"no integration named `{integration_name}`"])

    action = session.exec(
        select(Action).where(
            Action.integration_id == integration.id,
            Action.name == action_name,
        )
    ).first()
    if action is None:
        raise IntegrationError([f"integration `{integration_name}` has no action `{action_name}`"])

    # Checked now rather than at 3am when the rule first fires. Only the
    # required ones: an optional parameter left blank is the author's choice.
    missing = required_inputs(action) - set(parameters)
    if missing:
        raise IntegrationError(
            [
                f"action `{action_name}` needs {sorted(missing)}, which the binding "
                f"does not supply. Provide them as parameters, or reference a "
                f"column from the fired row."
            ]
        )

    existing = session.exec(
        select(RuleBinding).where(
            RuleBinding.workspace_id == workspace_id,
            RuleBinding.rule_name == rule_name,
            RuleBinding.action_id == action.id,
        )
    ).first()

    binding = existing or RuleBinding(
        workspace_id=workspace_id,
        rule_name=rule_name,
        action_id=action.id,
    )
    binding.parameters = parameters
    binding.recipient_template = recipient_template
    binding.enabled = True
    session.add(binding)
    session.flush()
    return binding


def action_parameters(action: Action) -> dict[str, ParameterSpec]:
    """What this action declares it needs.

    Falls back to the placeholders in its templates, typed as strings, for
    actions stored before parameters were declarable. The fallback keeps old
    rows working without a backfill; anything written since carries a real
    declaration.
    """
    declared = action.parameters or {}
    if declared:
        return {name: ParameterSpec.model_validate(spec) for name, spec in declared.items()}

    names = (
        templating.placeholders(action.body_template)
        | templating.placeholders(action.headers_template)
        | templating.placeholders(action.path_template)
    )
    return {name: ParameterSpec() for name in names if "." not in name and name not in ROW_CONTEXT}


def action_inputs(action: Action) -> set[str]:
    """The names a binding has to supply.

    Dotted names and row-context names are never among them: those are filled
    from the fired row at send time, not by whoever configured the binding.
    """
    return set(action_parameters(action))


def required_inputs(action: Action) -> set[str]:
    return {name for name, spec in action_parameters(action).items() if spec.required}


def bindings_for(
    session: Session,
    rule_name: str,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> list[tuple[RuleBinding, Action, Integration]]:
    """Everything a rule should do when it fires."""
    rows = session.exec(
        select(RuleBinding, Action, Integration)
        .join(Action, RuleBinding.action_id == Action.id)  # type: ignore[arg-type]
        .join(Integration, Action.integration_id == Integration.id)  # type: ignore[arg-type]
        .where(
            RuleBinding.workspace_id == workspace_id,
            RuleBinding.rule_name == rule_name,
            RuleBinding.enabled,  # type: ignore[arg-type]
            Integration.enabled,  # type: ignore[arg-type]
        )
        # Deterministic order, so a rule with several actions behaves the same
        # way every run rather than however the database felt.
        .order_by(RuleBinding.created_at)  # type: ignore[arg-type]
    ).all()
    return list(rows)
