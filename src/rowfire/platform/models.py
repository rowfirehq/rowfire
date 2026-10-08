"""Control-plane schema.

Separate from the customer's database in every sense: this one we own, write
to, and migrate. The customer's database stays read-only forever.

Single-tenant for now -- one deployment per design partner -- but every table
carries `workspace_id` anyway. That is not creeping multi-tenancy; it is one
column that keeps the eventual migration from being a unique-index rebuild on
`fire`, which will be the largest and most write-hot table here. Adding it
later means taking that rebuild while the ledger is live.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Every constraint gets a deterministic name, and this must be set before any
# table below is defined.
#
# Without it SQLAlchemy leaves foreign keys anonymous, Postgres invents its own
# names, and Alembic cannot emit DROP CONSTRAINT for them -- so every migration
# touching a foreign key becomes one-way. A migration you cannot reverse is a
# deploy you cannot roll back. Explicit names on individual constraints (the
# `uq_fire_dedup` the ledger relies on, for instance) still win over this.
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

# Single-tenant deployments all share this. It exists so the column is never
# null and the unique constraints are already the right shape.
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(**kwargs: Any) -> Column:
    """Always timestamptz. A naive timestamp in a scheduler is a future bug."""
    return Column(DateTime(timezone=True), **kwargs)


class Mode(StrEnum):
    """Shadow is the safe state, and the default everywhere.

    Promotion to live flips one flag and changes nothing else about how the
    automation is configured -- so what you validated in shadow is exactly
    what runs.
    """

    shadow = "shadow"
    live = "live"


class RunStatus(StrEnum):
    running = "running"
    ok = "ok"
    failed = "failed"


class DeliveryStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    suppressed = "suppressed"  # blocked by a frequency cap or kill switch


class Workspace(SQLModel, table=True):
    __tablename__ = "workspace"

    id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, primary_key=True)
    name: str = Field(default="default")
    # The kill switch from the concept doc: one flag that stops everything,
    # above every individual rule.
    halted: bool = Field(default=False)
    halted_reason: str | None = Field(default=None, sa_column=Column(Text))

    # The global frequency cap, which sits above every individual rule. Ten
    # individually correct automations still send one person ten messages.
    cap_per_recipient: int = Field(default=10)
    cap_window_hours: int = Field(default=24)

    # A hosted demo visitor's workspace: made on their first visit, and
    # deleted -- rules, history, sample data and all -- once idle for long
    # enough (see hosted.reap). A local install's workspace is never this.
    ephemeral: bool = Field(default=False)
    last_seen_at: datetime | None = Field(default=None, sa_column=_ts())

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class Connection(SQLModel, table=True):
    """A customer database credential, at rest.

    There is deliberately no plaintext column. The DSN only exists decrypted
    inside a worker's memory, for the length of one poll.
    """

    __tablename__ = "connection"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_connection_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    name: str = Field(default="primary")

    # --- envelope encryption; see crypto.py for why it is shaped this way ---
    dsn_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    dsn_nonce: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    wrapped_data_key: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    wrap_nonce: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    key_id: str = Field(default="local/env")
    algorithm: str

    statement_timeout_ms: int = Field(default=30_000)

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    last_verified_at: datetime | None = Field(default=None, sa_column=_ts())
    last_verify_error: str | None = Field(default=None, sa_column=Column(Text))


class DefinitionVersion(SQLModel, table=True):
    """An immutable snapshot of definitions.yaml.

    Versioned rather than updated in place because "any edit drops back to
    shadow" needs to know what changed, and because a bad edit to a live
    automation is precisely the moment you want the previous version back.
    """

    __tablename__ = "definition_version"
    __table_args__ = (UniqueConstraint("workspace_id", "version", name="uq_definition_version"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    version: int
    yaml_text: str = Field(sa_column=Column(Text, nullable=False))
    # The parsed form, so queries do not have to re-parse YAML.
    document: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    checksum: str = Field(index=True)
    note: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    created_by: str | None = Field(default=None)


class TriggerState(SQLModel, table=True):
    """Scheduling state for one trigger: where it has got to, and when next.

    Deliberately per *trigger*, not per rule. Several rules can hang off one
    trigger, and polling once and fanning the rows out to each of them is both
    cheaper for the customer's database and the only way the rules see a
    consistent snapshot.

    Whether something is live lives on RuleState instead -- one rule can be
    promoted while another on the same trigger is still in shadow.
    """

    __tablename__ = "trigger_state"
    __table_args__ = (
        UniqueConstraint("workspace_id", "trigger_name", name="uq_trigger_state_name"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    trigger_name: str

    enabled: bool = Field(default=True)

    # Connecting to 400,000 historical completed orders must not fire 400,000
    # events. Nothing before the watermark is ever eligible; it defaults to
    # "from now" when a trigger is first seen.
    watermark: datetime | None = Field(default=None, sa_column=_ts())

    connection_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("connection.id", ondelete="SET NULL")),
    )

    # --- scheduling ---
    poll_interval_seconds: int = Field(default=60)
    next_run_at: datetime | None = Field(default=None, sa_column=_ts())

    # Every poll re-scans this far behind the watermark. Rows can be written
    # with an event_time in the past (a backfill, a delayed job, a clock a few
    # seconds out), and a window that starts exactly at the watermark misses
    # them forever. The overlap is safe precisely because the ledger is
    # permanent: re-seeing a row costs one conflicting insert, not a duplicate
    # send. This is what the concept doc means by time windows becoming an
    # option rather than the mechanism.
    lookback_seconds: int = Field(default=600)

    # Lease, not a held transaction. A worker takes the row for a bounded
    # time; if it dies, the lease expires and another worker picks it up,
    # rather than a long-running transaction pinning the table.
    locked_until: datetime | None = Field(default=None, sa_column=_ts())
    locked_by: str | None = Field(default=None)

    last_run_at: datetime | None = Field(default=None, sa_column=_ts())
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class RuleState(SQLModel, table=True):
    """Per-rule state: is it live, and what was it reconciled against.

    Separate from TriggerState because one trigger feeds many rules and they
    are promoted independently -- notifying ops can go live while texting the
    customer is still being watched in shadow.
    """

    __tablename__ = "rule_state"
    __table_args__ = (UniqueConstraint("workspace_id", "rule_name", name="uq_rule_state_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    rule_name: str
    trigger_name: str = Field(index=True)

    mode: Mode = Field(default=Mode.shadow)
    enabled: bool = Field(default=True)

    # Covers the rule's own policy *and* the trigger it reads, so changing a
    # trigger's SQL demotes every rule that depends on it. Editing a live
    # automation is when people break things.
    definition_checksum: str | None = Field(default=None)
    definition_version_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("definition_version.id", ondelete="SET NULL")),
    )

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class Run(SQLModel, table=True):
    """One poll of one trigger. The audit trail for "why did this fire?"."""

    __tablename__ = "run"

    # Deliberately carries no mode. A poll fans out to every rule on the
    # trigger and those have modes of their own -- one may be live while its
    # sibling is still being watched. A single mode here could only ever be
    # wrong for somebody, and in practice it reported `shadow` for every run
    # ever made, including ones where a live rule sent.

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    trigger_name: str = Field(index=True)

    window_start: datetime | None = Field(default=None, sa_column=_ts())
    window_end: datetime | None = Field(default=None, sa_column=_ts())

    matched_rows: int = Field(default=0)
    fires_new: int = Field(default=0)
    fires_suppressed: int = Field(default=0)
    null_event_time_rows: int = Field(default=0)

    status: RunStatus = Field(default=RunStatus.running)
    error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    finished_at: datetime | None = Field(default=None, sa_column=_ts())


class Fire(SQLModel, table=True):
    """The permanent fire ledger.

    The unique constraint is the product's dedup guarantee, not a hint. An
    insert that conflicts is a fire that already happened, and the send is
    gated on the insert having actually inserted -- so two pollers racing the
    same row produce one fire and one no-op, decided by Postgres rather than
    by application logic.

    Keyed on the **rule**, not the trigger. If it keyed on the trigger, one
    rule firing for an order would mark it fired and a second rule on the same
    trigger would look it up, see "already", and silently never send -- one
    automation suppressing another.

    `dedup_bucket` generalises the policies onto one constraint:
      once_ever        -> ''            (one row per key, forever)
      once_per_period  -> '2026-08-03'  (one row per key per bucket)
      once_per_n       -> '7'           (the nth eligible occurrence)
    """

    __tablename__ = "fire"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "rule_name",
            "dedup_key",
            "dedup_bucket",
            name="uq_fire_dedup",
        ),
        # The "what fired recently" query behind the UI and the run report.
        Index("ix_fire_recent", "workspace_id", "rule_name", "fired_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID)
    rule_name: str

    dedup_key: str = Field(sa_column=Column(Text, nullable=False))
    dedup_bucket: str = Field(default="", sa_column=Column(Text, nullable=False))

    entity_id: str | None = Field(default=None, sa_column=Column(Text))
    event_time: datetime | None = Field(default=None, sa_column=_ts())
    fired_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))

    run_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(ForeignKey("run.id", ondelete="SET NULL"))
    )
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))


class DedupOccurrence(SQLModel, table=True):
    """Every occurrence once_per_n has already counted.

    Needed only by once_per_n, and needed for a specific reason. once_ever and
    once_per_period are pure uniqueness, so replaying a run is harmless -- the
    second attempt conflicts and does nothing. once_per_n *counts*, so a run
    that dies after incrementing but before sending would, on retry, count the
    same rows twice and shift every subsequent nth. This table makes the
    increment conditional on the occurrence being genuinely new.
    """

    __tablename__ = "dedup_occurrence"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "rule_name",
            "dedup_key",
            "occurrence_id",
            name="uq_dedup_occurrence",
        ),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID)
    rule_name: str
    dedup_key: str = Field(sa_column=Column(Text, nullable=False))
    # The identity of the underlying row -- normally the entity's primary key.
    occurrence_id: str = Field(sa_column=Column(Text, nullable=False))
    seen_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class DedupCounter(SQLModel, table=True):
    """Occurrence count per dedup key, for the once_per_n policy.

    Incremented atomically -- two workers must not both conclude they are the
    5th occurrence. Only ever advanced for occurrences that DedupOccurrence
    confirms are new.
    """

    __tablename__ = "dedup_counter"
    __table_args__ = (
        UniqueConstraint("workspace_id", "rule_name", "dedup_key", name="uq_dedup_counter"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID)
    rule_name: str
    dedup_key: str = Field(sa_column=Column(Text, nullable=False))
    seen_count: int = Field(default=0)
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class AuthKind(StrEnum):
    """How an integration proves who it is."""

    none = "none"
    bearer = "bearer"
    header = "header"
    # HTTP Basic, which is what an API *token* usually means in practice --
    # Zendesk, Twilio, Stripe and plenty of others. It needs two values, so
    # the integration names a second credential for the username half.
    basic = "basic"


class Integration(SQLModel, table=True):
    """A configured, named connection to an outside system.

    An *instance*, not a type: "Acme Slack" and "Support Slack" are two
    integrations that happen to share a provider. The name is what someone
    picks from a list when attaching an action, so it belongs to them.

    Deliberately data, not code. Slack is described in exactly the format a
    customer would use for their own API; if it needed special-casing in
    Python, the promise that anyone can add their own integration would be
    hollow. `provider` records which catalogue template it started from, or
    "custom" -- it is a label for the UI, never a branch in the engine.
    """

    __tablename__ = "integration"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_integration_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    name: str
    description: str | None = Field(default=None, sa_column=Column(Text))
    # Which catalogue template this came from, or "custom".
    provider: str = Field(default="custom")
    base_url: str = Field(sa_column=Column(Text, nullable=False))

    auth_kind: AuthKind = Field(default=AuthKind.none)
    # For auth_kind=header: which header the credential goes in.
    auth_header_name: str | None = Field(default=None)
    # Which entry of `credentials` carries the secret. Named rather than
    # assumed, because an integration may hold several -- an API key to
    # authenticate with and an account id that is merely configuration.
    auth_credential: str = Field(default="token")
    # For auth_kind=basic: which credential carries the username half.
    auth_username_credential: str | None = Field(default=None)

    # Every credential this integration holds, as one envelope-encrypted JSON
    # object: {"bot_token": "xoxb-..."}. A map rather than a single secret
    # because real APIs want more than one value, and discovering that later
    # would mean a migration on the most sensitive column in the schema.
    credentials_ciphertext: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    credentials_nonce: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    wrapped_data_key: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    wrap_nonce: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    key_id: str | None = Field(default=None)
    algorithm: str | None = Field(default=None)

    default_headers: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    timeout_ms: int = Field(default=10_000)
    enabled: bool = Field(default=True)

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class Action(SQLModel, table=True):
    """One operation on an integration: send a message, update a field.

    This is the REST call itself, described as data -- method, path, headers
    and body, with `{{ placeholders }}` for whatever the caller supplies. An
    action added through the UI and one loaded from a bundled template are the
    same row; there is no privileged path.
    """

    __tablename__ = "action"
    __table_args__ = (UniqueConstraint("integration_id", "name", name="uq_action_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    integration_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("integration.id", ondelete="CASCADE", name="fk_action_integration"),
            nullable=False,
        )
    )
    name: str
    description: str | None = Field(default=None, sa_column=Column(Text))

    method: str = Field(default="POST")
    path_template: str = Field(default="", sa_column=Column(Text))
    body_template: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    headers_template: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    # What a binding has to supply, declared rather than inferred from the
    # templates: {"channel": {"label": "Channel", "type": "string", ...}}.
    # Inference gives you names and nothing else -- no type to coerce by, no
    # label to ask with, and no way to tell a typo from a new input. Empty on
    # rows written before this existed, which `action_parameters` handles.
    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    retry_on: list[int] = Field(default_factory=list, sa_column=Column(JSONB))

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class RuleBinding(SQLModel, table=True):
    """Which action a rule fires, and with what parameters."""

    __tablename__ = "rule_binding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "rule_name", "action_id", name="uq_rule_binding"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    rule_name: str = Field(index=True)
    action_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("action.id", ondelete="CASCADE"), nullable=False)
    )
    # Templates resolved against the fired row, e.g. {"text": "order {{ id }}"}.
    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    # What the frequency cap counts against. Defaults to the binding itself.
    recipient_template: str | None = Field(default=None, sa_column=Column(Text))
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))


class Delivery(SQLModel, table=True):
    """What was actually sent, or would have been in shadow mode.

    A shadow delivery is recorded exactly like a live one, with mode set
    accordingly, so promoting a trigger changes one field rather than taking
    an untested code path.
    """

    __tablename__ = "delivery"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID, index=True)
    fire_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, ForeignKey("fire.id", ondelete="CASCADE")),
    )
    rule_name: str = Field(index=True)

    # The integration's name at send time, kept flat so the delivery log
    # still reads correctly after an integration is renamed or removed.
    channel: str = Field(default="slack")
    mode: Mode = Field(default=Mode.shadow)
    recipient: str | None = Field(default=None, index=True)

    action_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey(
                "action.id",
                ondelete="SET NULL",
                name="fk_delivery_action",
            )
        ),
    )
    # The request as it would be sent, WITHOUT authentication. Auth is applied
    # by the dispatcher at the moment of the call and never stored -- this
    # column is displayed in the UI, and a token rendered into it would be a
    # credential written to the database in plaintext.
    rendered: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    request_url: str | None = Field(default=None, sa_column=Column(Text))
    response_status: int | None = Field(default=None)

    status: DeliveryStatus = Field(default=DeliveryStatus.pending)
    attempts: int = Field(default=0)
    error: str | None = Field(default=None, sa_column=Column(Text))
    suppressed_reason: str | None = Field(default=None, sa_column=Column(Text))

    created_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
    sent_at: datetime | None = Field(default=None, sa_column=_ts())


class RecipientBudget(SQLModel, table=True):
    """The global frequency cap, which sits above every individual rule.

    Ten individually correct automations still send one person ten messages.
    This is counted per recipient per window across all triggers, which is why
    it is its own table rather than a query over `delivery`.
    """

    __tablename__ = "recipient_budget"
    __table_args__ = (
        UniqueConstraint("workspace_id", "recipient", "window_start", name="uq_recipient_budget"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    workspace_id: uuid.UUID = Field(default=DEFAULT_WORKSPACE_ID)
    recipient: str
    window_start: datetime = Field(sa_column=_ts(nullable=False))
    sent_count: int = Field(default=0)
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts(nullable=False))
