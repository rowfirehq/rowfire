"""The fire ledger: the one place that decides whether something is sent.

The rule is simple and the whole reliability story depends on it holding:

    a send happens if, and only if, `claim()` inserted a row.

Not "if the row looks new", not "if we checked and it wasn't there" -- if the
INSERT reported that it inserted. `SELECT` then `INSERT` has a window between
the two where a second worker does the same thing and both send; `INSERT …
ON CONFLICT DO NOTHING RETURNING` has no such window, because Postgres
resolves it under the unique index.

Risk #3 in the concept doc is that a duplicate SMS to 10,000 people is an
incident at the customer, caused by us. This module is the answer to it, which
is why the SQL here is written out rather than generated: it is short, it is
load-bearing, and it should be reviewable without knowing an ORM's defaults.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from ..dedup import period_bucket
from ..definitions import DedupPolicy, Rule, Trigger
from .models import DEFAULT_WORKSPACE_ID

Row = Mapping[str, Any]


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to claim a fire."""

    fired: bool
    dedup_key: str
    dedup_bucket: str
    fire_id: int | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.fired


def serialise_dedup_key(row: Row, key_columns: Sequence[str]) -> str:
    """A stable text identity for "the same thing happening".

    JSON rather than a delimiter join, so a value containing the delimiter
    cannot collide with a different key -- `["a|b"]` and `["a", "b"]` must not
    produce the same string, or two unrelated entities share a dedup slot and
    one of them silently never fires.
    """
    return json.dumps(
        [_normalise(row.get(column)) for column in key_columns],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _normalise(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        # str, not float: 1.10 and 1.1 must not become the same key.
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def bucket_for(rule: Rule, event_time: datetime | None, occurrence: int | None = None) -> str:
    """The bucket component of the unique key, per the rule's policy."""
    if rule.policy is DedupPolicy.once_ever:
        return ""
    if rule.policy is DedupPolicy.once_per_period:
        if event_time is None:
            # Cannot be bucketed, so cannot be deduplicated by period. The
            # caller must exclude these rather than let them all collide into
            # a single bucket and fire once.
            raise ValueError("once_per_period requires an event_time")
        assert rule.period is not None
        return period_bucket(event_time, rule.period).isoformat()
    if rule.policy is DedupPolicy.once_per_n:
        if occurrence is None:
            raise ValueError("once_per_n requires an occurrence ordinal")
        return str(occurrence)
    raise ValueError(f"unhandled dedup policy: {rule.policy}")


# ---------------------------------------------------------------- claiming

_INSERT_FIRE = text(
    """
    INSERT INTO fire (
        workspace_id, rule_name, dedup_key, dedup_bucket,
        entity_id, event_time, fired_at, run_id, payload
    )
    VALUES (
        :workspace_id, :rule_name, :dedup_key, :dedup_bucket,
        :entity_id, :event_time, now(), :run_id, CAST(:payload AS jsonb)
    )
    ON CONFLICT ON CONSTRAINT uq_fire_dedup DO NOTHING
    RETURNING id
    """
)

_CLAIM_OCCURRENCE = text(
    """
    INSERT INTO dedup_occurrence (
        workspace_id, rule_name, dedup_key, occurrence_id, seen_at
    )
    VALUES (:workspace_id, :rule_name, :dedup_key, :occurrence_id, now())
    ON CONFLICT ON CONSTRAINT uq_dedup_occurrence DO NOTHING
    RETURNING id
    """
)

_BUMP_COUNTER = text(
    """
    INSERT INTO dedup_counter (
        workspace_id, rule_name, dedup_key, seen_count, updated_at
    )
    VALUES (:workspace_id, :rule_name, :dedup_key, 1, now())
    ON CONFLICT ON CONSTRAINT uq_dedup_counter
    DO UPDATE SET seen_count = dedup_counter.seen_count + 1, updated_at = now()
    RETURNING seen_count
    """
)


def claim(
    session: Session,
    *,
    rule_name: str,
    trigger: Trigger,
    rule: Rule,
    row: Row,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> Claim:
    """Try to claim one fire. Send only when the result is truthy.

    Safe to call concurrently and safe to replay: the same row claimed twice
    yields one fired=True and one fired=False, in either order.
    """
    # The grain comes from the trigger, the cadence from the rule.
    dedup_key = serialise_dedup_key(row, trigger.key)
    event_time = row.get(trigger.event_time) if trigger.event_time else None
    entity_id = _normalise(row.get(trigger.key[0])) if trigger.key else None

    if rule.policy is DedupPolicy.once_per_period and event_time is None:
        # Reported, never silently folded into one bucket.
        return Claim(
            fired=False,
            dedup_key=dedup_key,
            dedup_bucket="",
            reason="no event_time, cannot be placed in a period bucket",
        )

    occurrence: int | None = None
    if rule.policy is DedupPolicy.once_per_n:
        if event_time is None:
            # once_per_n counts, so it must be able to tell one occurrence from
            # the next. Without a clock there is nothing to tell them apart,
            # and every row after the first would look like a replay -- which
            # is silently "fires once, ever" wearing the wrong name.
            return Claim(
                fired=False,
                dedup_key=dedup_key,
                dedup_bucket="",
                reason="once_per_n needs the trigger to declare an event_time",
            )
        occurrence = _next_occurrence(
            session,
            workspace_id=workspace_id,
            rule_name=rule_name,
            dedup_key=dedup_key,
            occurrence_id=_occurrence_id(dedup_key, event_time),
        )
        if occurrence is None:
            return Claim(
                fired=False,
                dedup_key=dedup_key,
                dedup_bucket="",
                reason="occurrence already counted",
            )
        assert rule.n is not None
        # Fire on the 1st, then every nth after it.
        if (occurrence - 1) % rule.n != 0:
            return Claim(
                fired=False,
                dedup_key=dedup_key,
                dedup_bucket=str(occurrence),
                reason=f"occurrence {occurrence} is not an nth (n={rule.n})",
            )

    bucket = bucket_for(rule, event_time, occurrence)

    result = session.execute(
        _INSERT_FIRE,
        {
            "workspace_id": workspace_id,
            "rule_name": rule_name,
            "dedup_key": dedup_key,
            "dedup_bucket": bucket,
            "entity_id": None if entity_id is None else str(entity_id),
            "event_time": event_time,
            "run_id": run_id,
            "payload": json.dumps({k: _normalise(v) for k, v in row.items()}, ensure_ascii=False),
        },
    ).first()

    if result is None:
        return Claim(
            fired=False,
            dedup_key=dedup_key,
            dedup_bucket=bucket,
            reason="already fired",
        )

    return Claim(fired=True, dedup_key=dedup_key, dedup_bucket=bucket, fire_id=int(result[0]))


def _occurrence_id(dedup_key: str, event_time: datetime) -> str:
    """What makes this occurrence distinct from the next one at the same key.

    once_per_n has to tell "a new order from this customer" apart from "the
    same order seen again by the lookback overlap". The trigger declares
    exactly two things that can answer that: its key and its clock.

    Not the whole row. A mutable column -- an order's status, a total that gets
    corrected -- would differ between polls, the same occurrence would be
    counted twice, and every subsequent nth would shift. And not the key alone
    either: the key *is* the counting bucket, so every row would look like a
    repeat. That was the bug: the identity was taken from the key's first
    column, so a trigger keyed on the customer counted one occurrence ever.

    The known limit: two rows sharing a key and a timestamp to the microsecond
    count once. By the trigger's own declaration they are the same occurrence,
    which is a definition rather than a miscount.
    """
    return f"{dedup_key}@{event_time.isoformat()}"


def _next_occurrence(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    rule_name: str,
    dedup_key: str,
    occurrence_id: str,
) -> int | None:
    """Count this occurrence once, ever. None means it was already counted."""
    claimed = session.execute(
        _CLAIM_OCCURRENCE,
        {
            "workspace_id": workspace_id,
            "rule_name": rule_name,
            "dedup_key": dedup_key,
            "occurrence_id": occurrence_id,
        },
    ).first()

    if claimed is None:
        return None

    bumped = session.execute(
        _BUMP_COUNTER,
        {
            "workspace_id": workspace_id,
            "rule_name": rule_name,
            "dedup_key": dedup_key,
        },
    ).first()
    return int(bumped[0])


def has_fired(
    session: Session,
    *,
    rule_name: str,
    dedup_key: str,
    dedup_bucket: str = "",
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> bool:
    """Read-only check, for reporting only.

    Never gate a send on this: between the check and the send another worker
    can claim the same fire. Gate on `claim()`.
    """
    found = session.execute(
        text(
            "SELECT 1 FROM fire WHERE workspace_id = :workspace_id "
            "AND rule_name = :rule_name AND dedup_key = :dedup_key "
            "AND dedup_bucket = :dedup_bucket LIMIT 1"
        ),
        {
            "workspace_id": workspace_id,
            "rule_name": rule_name,
            "dedup_key": dedup_key,
            "dedup_bucket": dedup_bucket,
        },
    ).first()
    return found is not None
