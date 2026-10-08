"""Definitions parsing and structural validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rowfire.definitions import (
    DedupPolicy,
    DefinitionError,
    Definitions,
    Rule,
    load,
)

MINIMAL = """
version: 2
source:
  dsn_env: DATABASE_URL
triggers:
  order_completed:
    description: An order finished
    sql: |
      SELECT id, customer_id, completed_at FROM orders WHERE status = 4
    event_time: completed_at
    key: [id]
rules:
  tell_ops:
    trigger: order_completed
    policy: once_ever
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "definitions.yaml"
    path.write_text(text)
    return path


def test_loads_a_minimal_file(tmp_path: Path) -> None:
    defs = load(write(tmp_path, MINIMAL))
    assert "FROM orders" in defs.triggers["order_completed"].sql
    assert defs.triggers["order_completed"].key == ["id"]
    assert defs.rules["tell_ops"].trigger == "order_completed"
    assert defs.source.statement_timeout_ms == 30_000


def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError) as excinfo:
        load(tmp_path / "nope.yaml")
    assert "not found" in excinfo.value.errors[0]


def test_empty_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="empty"):
        load(write(tmp_path, ""))


def test_malformed_yaml_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="not valid YAML"):
        load(write(tmp_path, "version: 2\n  bad indent: ["))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    # extra="forbid" everywhere: a typo'd key should not be silently ignored.
    text = MINIMAL.replace("    key: [id]", "    key: [id]\n    kay: [id]")
    with pytest.raises(DefinitionError) as excinfo:
        load(write(tmp_path, text))
    assert any("kay" in e for e in excinfo.value.errors)


def test_rule_referencing_unknown_trigger(tmp_path: Path) -> None:
    text = MINIMAL.replace("    trigger: order_completed", "    trigger: ghost")
    with pytest.raises(DefinitionError) as excinfo:
        load(write(tmp_path, text))
    assert any("ghost" in e for e in excinfo.value.errors)
    # And it says what the real options were, rather than only what was wrong.
    assert any("order_completed" in e for e in excinfo.value.errors)


def test_a_trigger_needs_a_key(tmp_path: Path) -> None:
    # Dedup runs over the returned rows, so an empty key would collapse
    # everything into a single fire. Rejected at parse time instead.
    text = MINIMAL.replace("    key: [id]", "    key: []")
    with pytest.raises(DefinitionError):
        load(write(tmp_path, text))


def test_version_1_is_rejected_by_name(tmp_path: Path) -> None:
    # Stored versions are immutable snapshots, so a format change leaves older
    # ones unreadable. The reason should not be a puzzle.
    with pytest.raises(DefinitionError) as excinfo:
        load(write(tmp_path, "version: 1\nentities: {}\ntriggers: {}\n"))
    message = " ".join(excinfo.value.errors)
    assert "version 1" in message and "version 2" in message


def test_unsupported_future_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Definitions.model_validate({"version": 3, "triggers": {}, "rules": {}})


def test_event_time_is_optional(tmp_path: Path) -> None:
    # A trigger without one still parses; it just cannot be placed in time.
    text = MINIMAL.replace("    event_time: completed_at\n", "")
    defs = load(write(tmp_path, text))
    assert defs.triggers["order_completed"].event_time is None


def test_once_per_period_requires_a_period() -> None:
    with pytest.raises(ValueError, match="requires `period`"):
        Rule(trigger="t", policy=DedupPolicy.once_per_period)


def test_once_per_n_requires_n() -> None:
    with pytest.raises(ValueError, match="requires `n`"):
        Rule(trigger="t", policy=DedupPolicy.once_per_n)


def test_period_is_rejected_on_the_wrong_policy() -> None:
    # Silently ignoring it would mean a rule that reads weekly and fires
    # every time.
    with pytest.raises(ValueError, match="only valid with policy: once_per_period"):
        Rule(trigger="t", policy=DedupPolicy.once_ever, period="week")


def test_n_is_rejected_on_the_wrong_policy() -> None:
    with pytest.raises(ValueError, match="only valid with policy: once_per_n"):
        Rule(trigger="t", policy=DedupPolicy.once_ever, n=3)


def test_rules_for_returns_only_that_triggers_rules(definitions: Definitions) -> None:
    assert sorted(definitions.rules_for("customer_ordered")) == [
        "nudge_ops",
        "thank_the_customer",
    ]
    assert sorted(definitions.rules_for("completed_orders")) == ["tell_ops"]
    assert definitions.rules_for("nothing") == {}
