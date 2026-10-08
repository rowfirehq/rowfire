"""Reads and writes over the control-plane tables.

Everything that decides *what should run* lives here; scheduler.py decides
*when*, and ledger.py decides *whether it has already happened*.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import yaml
from sqlalchemy import text
from sqlmodel import Session, select

from ..definitions import Definitions
from ..definitions import loads as loads_definitions
from . import crypto
from .models import (
    DEFAULT_WORKSPACE_ID,
    Connection,
    DefinitionVersion,
    Mode,
    RuleState,
    TriggerState,
    Workspace,
)


class StoreError(Exception):
    """Raised for control-plane state problems. Never carries a credential."""


# The source name every install used before there could be more than one, and
# the one a trigger with no `source:` reads when it exists.
DEFAULT_SOURCE = "primary"


# ------------------------------------------------------------- workspace


def ensure_workspace(session: Session, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        workspace = Workspace(id=workspace_id)
        session.add(workspace)
        session.flush()
    return workspace


# ------------------------------------------------------------ connections


def save_connection(
    session: Session,
    dsn: str,
    *,
    name: str = DEFAULT_SOURCE,
    statement_timeout_ms: int = 30_000,
    master_key: bytes | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> Connection:
    """Store a customer DSN, encrypted. The plaintext is never persisted."""
    ensure_workspace(session, workspace_id)
    envelope = crypto.encrypt(dsn, master_key=master_key)

    existing = session.exec(
        select(Connection).where(Connection.workspace_id == workspace_id, Connection.name == name)
    ).first()

    if existing is None:
        existing = Connection(workspace_id=workspace_id, name=name, algorithm=envelope.algorithm)
        session.add(existing)

    existing.dsn_ciphertext = envelope.ciphertext
    existing.dsn_nonce = envelope.nonce
    existing.wrapped_data_key = envelope.wrapped_data_key
    existing.wrap_nonce = envelope.wrap_nonce
    existing.key_id = envelope.key_id
    existing.algorithm = envelope.algorithm
    existing.statement_timeout_ms = statement_timeout_ms
    session.flush()
    return existing


def reveal_dsn(connection: Connection, master_key: bytes | None = None) -> str:
    """Decrypt a stored DSN.

    The result must not be logged, echoed in an error, or returned over HTTP.
    engine.connect() already strips DSN-shaped text from its exceptions.
    """
    envelope = crypto.Envelope(
        ciphertext=connection.dsn_ciphertext,
        nonce=connection.dsn_nonce,
        wrapped_data_key=connection.wrapped_data_key,
        wrap_nonce=connection.wrap_nonce,
        key_id=connection.key_id,
        algorithm=connection.algorithm,
    )
    return crypto.decrypt(envelope, master_key=master_key)


def get_connection(
    session: Session,
    *,
    name: str = DEFAULT_SOURCE,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> Connection | None:
    return session.exec(
        select(Connection).where(Connection.workspace_id == workspace_id, Connection.name == name)
    ).first()


def list_connections(
    session: Session, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> list[Connection]:
    """Every stored data source, in name order."""
    return list(
        session.exec(
            select(Connection)
            .where(Connection.workspace_id == workspace_id)
            .order_by(Connection.name)  # type: ignore[arg-type]
        ).all()
    )


def delete_connection(
    session: Session, name: str, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> bool:
    connection = get_connection(session, name=name, workspace_id=workspace_id)
    if connection is None:
        return False
    session.delete(connection)
    session.flush()
    return True


def default_source(
    session: Session, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> str | None:
    """Which source a trigger that names none reads.

    `primary` when there is one -- it is what every install before multiple
    sources stored its connection as. Otherwise the first source added.

    "The first added" rather than "the only one" on purpose: with the latter,
    adding a second source would leave every trigger that names none with no
    default at all, and they would stop polling without anything about them
    having changed. Adding a source must never change what an existing trigger
    reads.
    """
    connections = list_connections(session, workspace_id=workspace_id)
    if not connections:
        return None
    if any(c.name == DEFAULT_SOURCE for c in connections):
        return DEFAULT_SOURCE
    return min(connections, key=lambda c: (c.created_at, c.name)).name


# ------------------------------------------------------------ definitions


def trigger_checksum(definitions: Definitions, trigger_name: str) -> str:
    """A fingerprint of everything that changes what a trigger *means*."""
    trigger = definitions.triggers[trigger_name]
    material: dict[str, object] = {
        "sql": trigger.sql,
        "event_time": trigger.event_time,
        "key": trigger.key,
    }
    # Pointing the same query at a different database changes what it means.
    # Only folded in when set, so a trigger that names no source keeps the
    # checksum it had before sources existed and is not demoted by an upgrade.
    if trigger.source:
        material["source"] = trigger.source
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def rule_checksum(definitions: Definitions, rule_name: str) -> str:
    """A fingerprint of a rule, including the trigger it reads.

    Folding the trigger in is what makes a change to a query demote every rule
    that depends on it -- the rule itself did not change, but what it fires on
    did, which is the same thing from the operator's point of view.

    Deliberately narrow in the other direction: editing one rule leaves the
    others alone. A demotion nobody can predict is a demotion everybody learns
    to ignore.
    """
    rule = definitions.rules[rule_name]
    material = {
        "trigger": rule.trigger,
        "trigger_checksum": trigger_checksum(definitions, rule.trigger),
        "policy": rule.policy.value,
        "period": rule.period,
        "n": rule.n,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def save_definitions(
    session: Session,
    yaml_text: str,
    *,
    note: str | None = None,
    created_by: str | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> DefinitionVersion:
    """Validate, then store as a new immutable version."""
    definitions = loads_definitions(yaml_text, label="definitions")
    ensure_workspace(session, workspace_id)

    latest = session.exec(
        select(DefinitionVersion)
        .where(DefinitionVersion.workspace_id == workspace_id)
        .order_by(DefinitionVersion.version.desc())  # type: ignore[attr-defined]
    ).first()

    version = DefinitionVersion(
        workspace_id=workspace_id,
        version=1 if latest is None else latest.version + 1,
        yaml_text=yaml_text,
        document=yaml.safe_load(yaml_text) or {},
        checksum=hashlib.sha256(yaml_text.encode()).hexdigest()[:32],
        note=note,
        created_by=created_by,
    )
    session.add(version)
    session.flush()

    reconcile(session, definitions, version, workspace_id=workspace_id)
    return version


def active_definitions(
    session: Session, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> tuple[DefinitionVersion, Definitions] | None:
    row = session.exec(
        select(DefinitionVersion)
        .where(DefinitionVersion.workspace_id == workspace_id)
        .order_by(DefinitionVersion.version.desc())  # type: ignore[attr-defined]
    ).first()
    if row is None:
        return None
    return row, loads_definitions(row.yaml_text, label=f"version {row.version}")


def require_definitions(
    session: Session, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> tuple[int, Definitions]:
    """The definitions in force, or a clear error saying nothing is stored.

    Returns the version *number* rather than the row: callers are usually CLI
    commands that print it after the session has closed, and an ORM instance
    expires on commit.
    """
    found = active_definitions(session, workspace_id=workspace_id)
    if found is None:
        raise StoreError(
            "no definitions stored yet. Run `rowfire init` to generate them "
            "from the schema, or `rowfire platform push -f <file>` to import "
            "a set you already have."
        )
    version, definitions = found
    return version.version, definitions


def customer_dsn(
    session: Session,
    definitions: Definitions,
    *,
    source: str | None = None,
    master_key: bytes | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> str:
    """How to reach one of the customer's databases.

    `source` is a trigger's `source:`. Named, it must be a stored connection.
    Unnamed, it is the default source (see `default_source`).

    The stored connection wins, because it is the one the worker uses -- a CLI
    that read a different database from the scheduler would answer questions
    about the wrong data while looking perfectly healthy. The environment
    variable named in the definitions remains the fallback for an unnamed
    source, so a fresh install with no stored connection still works.
    """
    if source is not None:
        connection = get_connection(session, name=source, workspace_id=workspace_id)
        if connection is None:
            known = ", ".join(c.name for c in list_connections(session, workspace_id=workspace_id))
            raise StoreError(
                f"no data source named `{source}`. Stored sources: {known or 'none yet'}."
            )
        return reveal_dsn(connection, master_key=master_key)

    name = default_source(session, workspace_id=workspace_id)
    if name is not None:
        connection = get_connection(session, name=name, workspace_id=workspace_id)
        if connection is not None:
            return reveal_dsn(connection, master_key=master_key)

    from ..engine import EngineError, resolve_dsn

    try:
        return resolve_dsn(definitions)
    except EngineError as exc:
        raise StoreError(f"no database connection stored, and {exc}") from exc


def trigger_dsn(
    session: Session,
    definitions: Definitions,
    trigger_name: str,
    *,
    master_key: bytes | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> str:
    """The DSN of the source a named trigger reads."""
    trigger = definitions.triggers.get(trigger_name)
    if trigger is None:
        raise StoreError(f"no trigger named `{trigger_name}`")
    return customer_dsn(
        session,
        definitions,
        source=trigger.source,
        master_key=master_key,
        workspace_id=workspace_id,
    )


# --------------------------------------------------------- trigger state


def reconcile(
    session: Session,
    definitions: Definitions,
    version: DefinitionVersion,
    *,
    now: datetime | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> dict[str, str]:
    """Bring trigger scheduling and rule state in line with a version.

    Returns {name: what happened}, so a caller can tell the user which of
    their live automations just got demoted and why.
    """
    moment = now or datetime.now(UTC)
    outcomes: dict[str, str] = {}

    triggers = {
        state.trigger_name: state
        for state in session.exec(
            select(TriggerState).where(TriggerState.workspace_id == workspace_id)
        ).all()
    }
    rules = {
        state.rule_name: state
        for state in session.exec(
            select(RuleState).where(RuleState.workspace_id == workspace_id)
        ).all()
    }

    # --- triggers carry scheduling only ---------------------------------
    for name in definitions.triggers:
        state = triggers.get(name)
        if state is None:
            # Cold start: the watermark is *now*, never the beginning of time.
            # Connecting to 400,000 historical completed orders must not fire
            # 400,000 events.
            session.add(
                TriggerState(
                    workspace_id=workspace_id,
                    trigger_name=name,
                    watermark=moment,
                    next_run_at=moment,
                )
            )
            outcomes[f"trigger {name}"] = "created (watermark=now)"
        else:
            if not state.enabled:
                state.enabled = True
                state.updated_at = moment
            outcomes[f"trigger {name}"] = "unchanged"

    for name, state in triggers.items():
        if name not in definitions.triggers and state.enabled:
            state.enabled = False
            state.updated_at = moment
            outcomes[f"trigger {name}"] = "disabled (no longer defined)"

    # --- rules carry mode ------------------------------------------------
    for name, rule in definitions.rules.items():
        checksum = rule_checksum(definitions, name)
        state = rules.get(name)

        if state is None:
            session.add(
                RuleState(
                    workspace_id=workspace_id,
                    rule_name=name,
                    trigger_name=rule.trigger,
                    mode=Mode.shadow,
                    definition_checksum=checksum,
                    definition_version_id=version.id,
                )
            )
            outcomes[name] = "created (shadow)"
            continue

        state.trigger_name = rule.trigger
        state.definition_version_id = version.id
        state.updated_at = moment

        if not state.enabled:
            # Deleted and brought back under the same name. Without this it
            # stays disabled forever: present in the definitions, listed in
            # the UI with everything else, and silently dropped from every
            # poll. The trigger branch above has always re-enabled; rules did
            # not, which made a re-created rule a thing that looks configured
            # and never fires.
            #
            # It returns in shadow regardless of how it left, like anything
            # else whose behaviour nobody has watched recently.
            state.enabled = True
            state.mode = Mode.shadow
            state.definition_checksum = checksum
            outcomes[name] = "re-enabled (shadow)"
            continue

        if state.definition_checksum != checksum:
            # Editing a live automation -- or the query it reads -- is when
            # people break things, so it always costs live status.
            was_live = state.mode is Mode.live
            state.mode = Mode.shadow
            state.definition_checksum = checksum
            outcomes[name] = "demoted to shadow (definition changed)" if was_live else "updated"
        else:
            outcomes[name] = "unchanged"

    for name, state in rules.items():
        if name not in definitions.rules and state.enabled:
            state.enabled = False
            state.updated_at = moment
            outcomes[name] = "disabled (no longer defined)"

    session.flush()
    return outcomes


def set_mode(
    session: Session,
    rule_name: str,
    mode: Mode,
    *,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> RuleState:
    """Promote a rule to live, or return it to shadow."""
    state = session.exec(
        select(RuleState).where(
            RuleState.workspace_id == workspace_id, RuleState.rule_name == rule_name
        )
    ).first()
    if state is None:
        raise StoreError(f"no rule named `{rule_name}`")
    state.mode = mode
    state.updated_at = datetime.now(UTC)
    session.flush()
    return state


def halt(
    session: Session,
    reason: str,
    *,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> Workspace:
    """The kill switch. One flag, above every individual rule."""
    workspace = ensure_workspace(session, workspace_id)
    workspace.halted = True
    workspace.halted_reason = reason
    session.flush()
    return workspace


def resume(session: Session, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID) -> Workspace:
    workspace = ensure_workspace(session, workspace_id)
    workspace.halted = False
    workspace.halted_reason = None
    session.flush()
    return workspace


# ------------------------------------------------------- frequency caps

_BUMP_BUDGET = text(
    """
    INSERT INTO recipient_budget (
        workspace_id, recipient, window_start, sent_count, updated_at
    )
    VALUES (:workspace_id, :recipient, :window_start, 1, now())
    ON CONFLICT ON CONSTRAINT uq_recipient_budget
    DO UPDATE SET sent_count = recipient_budget.sent_count + 1, updated_at = now()
    RETURNING sent_count
    """
)


def consume_budget(
    session: Session,
    recipient: str,
    *,
    cap: int,
    window_hours: int,
    now: datetime | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> tuple[bool, int]:
    """Claim one message against a recipient's budget.

    Returns (allowed, count_after). Incremented atomically for the same reason
    the ledger is: two workers must not both see "9 of 10" and both send.
    """
    moment = now or datetime.now(UTC)
    window = moment.replace(minute=0, second=0, microsecond=0)
    window = window.replace(hour=(window.hour // window_hours) * window_hours)

    count = session.execute(
        _BUMP_BUDGET,
        {"workspace_id": workspace_id, "recipient": recipient, "window_start": window},
    ).scalar_one()
    return count <= cap, int(count)
