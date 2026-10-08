"""Pydantic models for definitions.yaml, plus loading and validation.

Two concepts, deliberately separate:

  Trigger   a SQL query that returns the rows an event happened on, plus the
            column that places each row in time and the columns that identify
            one row. Written by whoever knows the schema.

  Rule      picks a trigger and says how often it may fire for the same
            identity, then hangs actions off it.

The split follows what each author actually knows. The grain -- "one row per
order, identified by id" -- is a fact about the query, and only the person who
wrote the SELECT reliably knows it. The cadence -- once ever, once a week --
is a property of what you are doing with the event, and the same query
legitimately wants different answers for different actions.

Definitions are plain data by design: an LLM-drafted trigger goes through
exactly this validation path as a hand-written one.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

FORMAT_VERSION = 2


class DefinitionError(Exception):
    """Raised when a definitions file cannot be parsed or is incoherent.

    Carries a list of messages so the CLI can report every problem at once
    rather than making the user fix them one per run.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


class SemanticType(StrEnum):
    """What a column *means*, not how Postgres stores it.

    Used for display -- a `money` renders with its decimals, a `timestamp`
    without microseconds -- and as the vocabulary a later enrichment or
    semantic layer would speak.
    """

    string = "string"
    integer = "integer"
    money = "money"
    timestamp = "timestamp"
    boolean = "boolean"
    enum = "enum"
    phone = "phone"
    email = "email"
    url = "url"
    json = "json"
    uuid = "uuid"
    identifier = "identifier"
    unknown = "unknown"


class DedupPolicy(StrEnum):
    once_ever = "once_ever"
    once_per_period = "once_per_period"
    once_per_n = "once_per_n"


Period = Literal["day", "week", "month"]


class Trigger(BaseModel):
    """A SQL query describing when something happened.

    Not scoped to an entity: a trigger may join whatever it needs, which is
    why enrichment is not a separate feature here -- a joined column is just
    a column.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = None

    # The data source this query reads: the name of a stored connection, such
    # as `primary` or `support_mysql`. Omitted means the default source, so a
    # file written before there could be more than one still reads the same
    # database it always did.
    source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")

    # Any single SELECT. See compile.py for what is and is not allowed, and
    # for why the boundary is deliberately looser than it used to be.
    sql: str = Field(min_length=1)

    # The output column that places a row in time. Required for live polling;
    # without it a backtest can report totals but no distribution.
    event_time: str | None = None

    # The grain: which output columns identify one row. A fact about the
    # query, which is why it lives here and not on the rules that use it.
    key: list[str] = Field(min_length=1)


class Rule(BaseModel):
    """What to do when a trigger's rows appear, and how often."""

    model_config = ConfigDict(extra="forbid")

    trigger: str
    description: str | None = None

    policy: DedupPolicy = DedupPolicy.once_ever
    period: Period | None = None
    n: int | None = None

    @model_validator(mode="after")
    def _policy_requires_its_parameter(self) -> Rule:
        if self.policy is DedupPolicy.once_per_period and self.period is None:
            raise ValueError("policy: once_per_period requires `period`")
        if self.policy is DedupPolicy.once_per_n:
            if self.n is None:
                raise ValueError("policy: once_per_n requires `n`")
            if self.n < 1:
                raise ValueError("`n` must be >= 1")
        if self.policy is not DedupPolicy.once_per_period and self.period is not None:
            raise ValueError("`period` is only valid with policy: once_per_period")
        if self.policy is not DedupPolicy.once_per_n and self.n is not None:
            raise ValueError("`n` is only valid with policy: once_per_n")
        return self


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Informational: the driver is chosen from the DSN's scheme, which is the
    # one thing that cannot disagree with the database it points at.
    type: Literal["postgres", "mysql"] = "postgres"
    # Name of the env var holding the DSN for a trigger that names no stored
    # source. The DSN itself is never written to this file, never logged, and
    # never rendered in output.
    dsn_env: str = "DATABASE_URL"
    statement_timeout_ms: int = 30_000
    # A hard cap on rows returned by one poll, so a mis-written join cannot
    # pull a table into memory.
    max_rows: int = 50_000


class Definitions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    source: Source = Field(default_factory=Source)
    triggers: dict[str, Trigger] = Field(default_factory=dict)
    rules: dict[str, Rule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> Definitions:
        errors: list[str] = []
        for rule_name, rule in self.rules.items():
            if rule.trigger not in self.triggers:
                known = ", ".join(sorted(self.triggers)) or "none defined"
                errors.append(
                    f"rule `{rule_name}`: unknown trigger `{rule.trigger}`. Known triggers: {known}"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def rules_for(self, trigger_name: str) -> dict[str, Rule]:
        """Every rule hanging off one trigger, in name order."""
        return {
            name: rule for name, rule in sorted(self.rules.items()) if rule.trigger == trigger_name
        }


def loads(text: str, label: str = "definitions") -> Definitions:
    """Validate definitions from a string.

    Split out from `load` so a caller can check content *before* writing
    anything -- the UI needs this, and when definitions.yaml is bind-mounted
    into a container as a single file there is no writable directory beside it
    to hold a temporary copy.
    """
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DefinitionError([f"{label} is not valid YAML: {exc}"]) from exc

    if raw is None:
        raise DefinitionError([f"{label} is empty"])
    if not isinstance(raw, dict):
        raise DefinitionError([f"{label} must contain a YAML mapping at the top level"])

    # Say so plainly rather than letting pydantic report a confusing field
    # error. Stored versions are immutable snapshots, so a format change
    # leaves older ones unreadable and the reason should not be a puzzle.
    version = raw.get("version")
    if version is not None and version != FORMAT_VERSION:
        raise DefinitionError(
            [
                f"{label} is format version {version}; this build reads version "
                f"{FORMAT_VERSION}. Version 1 described entities and put the "
                f"predicate on a trigger; version 2 replaces that with a SQL "
                f"query per trigger and separate rules."
            ]
        )

    try:
        return Definitions.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, or our own ValueError
        raise DefinitionError(_flatten_validation_error(exc)) from exc


def load(path: str | Path) -> Definitions:
    """Read and validate a definitions file.

    Raises DefinitionError with every problem found, rather than the first.
    """
    path = Path(path)
    if not path.exists():
        raise DefinitionError([f"definitions file not found: {path}"])
    return loads(path.read_text(), label=str(path))


def _flatten_validation_error(exc: Exception) -> list[str]:
    """Turn a pydantic ValidationError into readable one-line messages."""
    errors_fn = getattr(exc, "errors", None)
    if not callable(errors_fn):
        return [str(exc)]

    messages: list[str] = []
    for err in errors_fn():
        location = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        messages.append(f"{location}: {err.get('msg', 'invalid')}")
    return messages or [str(exc)]
