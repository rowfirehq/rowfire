"""The poll loop: which trigger runs next, and what happens when it does.

Leases rather than held transactions. A worker claims a trigger for a bounded
time with `FOR UPDATE SKIP LOCKED`, commits immediately, and then does the slow
part (querying the customer's database) outside any lock. If the worker dies,
the lease expires and another picks it up. Holding a transaction open for the
duration of a poll would work until the first slow query, then pin the table.

Window semantics, which are the part most likely to be got subtly wrong:

    since = watermark - lookback
    until = now

The overlap is deliberate. Rows arrive with event_times slightly in the past --
a delayed job, a backfill, a clock a few seconds out -- and a window starting
exactly at the watermark misses them permanently. Re-scanning is safe because
the ledger is permanent: a row seen twice costs one conflicting insert, not a
duplicate send. This is what makes the ledger the mechanism and the time window
merely an optimisation.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from .. import engine as customer_db
from .. import sources
from ..compile import CompileError, compile_query
from ..definitions import Definitions
from . import ledger, store
from .models import (
    DEFAULT_WORKSPACE_ID,
    Mode,
    RuleState,
    Run,
    RunStatus,
    TriggerState,
    Workspace,
)

WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


@dataclass
class RuleOutcome:
    rule_name: str
    mode: Mode
    fires_new: int = 0
    fires_suppressed: int = 0


@dataclass
class PollOutcome:
    """One poll of one trigger, and what each of its rules did with the rows."""

    trigger_name: str
    matched_rows: int = 0
    null_event_time_rows: int = 0
    rules: list[RuleOutcome] = field(default_factory=list)
    deliveries: list[Any] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None

    @property
    def fires_new(self) -> int:
        return sum(r.fires_new for r in self.rules)

    @property
    def fires_suppressed(self) -> int:
        return sum(r.fires_suppressed for r in self.rules)


_LEASE_NEXT_SQL = """
    UPDATE trigger_state
    SET locked_until = now() + make_interval(secs => :lease_seconds),
        locked_by = :worker
    WHERE id = (
        SELECT id FROM trigger_state
        WHERE {scope}
          AND enabled
          AND (locked_until IS NULL OR locked_until < now())
          AND (next_run_at IS NULL OR next_run_at <= now())
        ORDER BY next_run_at NULLS FIRST
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
_LEASE_NEXT = text(_LEASE_NEXT_SQL.format(scope="workspace_id = :workspace_id"))
# Any workspace, most overdue first: what a worker serving several workspaces
# (a hosted demo's visitors) runs, so none of them waits behind another.
_LEASE_NEXT_ANY = text(_LEASE_NEXT_SQL.format(scope="true"))


def lease_next(
    session: Session,
    *,
    lease_seconds: int = 300,
    worker: str = WORKER_ID,
    workspace_id: uuid.UUID | None = DEFAULT_WORKSPACE_ID,
) -> TriggerState | None:
    """Claim the next due trigger, or None if nothing is due.

    SKIP LOCKED is what lets several workers run without coordinating: each
    takes a different row rather than queueing behind the same one.
    `workspace_id=None` takes the most overdue trigger in any workspace.
    """
    params: dict[str, Any] = {"lease_seconds": lease_seconds, "worker": worker}
    if workspace_id is None:
        row = session.execute(_LEASE_NEXT_ANY, params).first()
    else:
        row = session.execute(_LEASE_NEXT, {**params, "workspace_id": workspace_id}).first()
    if row is None:
        return None
    session.commit()
    return session.get(TriggerState, row[0])


def release(
    session: Session,
    state: TriggerState,
    *,
    now: datetime | None = None,
    advance_watermark_to: datetime | None = None,
    error: str | None = None,
) -> None:
    """Drop the lease and schedule the next run."""
    moment = now or datetime.now(UTC)
    state.locked_until = None
    state.locked_by = None
    state.last_run_at = moment
    state.last_error = error
    state.next_run_at = moment + timedelta(seconds=state.poll_interval_seconds)
    if advance_watermark_to is not None:
        state.watermark = advance_watermark_to
    state.updated_at = moment
    session.add(state)
    session.commit()


def poll(
    session: Session,
    state: TriggerState,
    definitions: Definitions,
    dsn: str | None,
    *,
    now: datetime | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
    deliver: Any = None,
    master_key: bytes | None = None,
) -> PollOutcome:
    """Run one trigger once, then fan its rows out to every rule on it.

    One query, N rules. Polling per rule would run the same SELECT several
    times against the customer's replica and let two rules see different
    snapshots of it.
    """
    moment = now or datetime.now(UTC)
    outcome = PollOutcome(trigger_name=state.trigger_name)

    workspace = store.ensure_workspace(session, workspace_id)
    if workspace.halted:
        outcome.skipped = f"workspace halted: {workspace.halted_reason or 'no reason given'}"
        return outcome

    trigger = definitions.triggers.get(state.trigger_name)
    if trigger is None:
        outcome.skipped = "trigger is no longer defined"
        return outcome

    if not trigger.event_time:
        # Without a time column there is no window, so every poll would
        # re-evaluate the whole query. Refused rather than silently doing it.
        outcome.error = f"trigger `{state.trigger_name}` has no event_time; live polling needs one"
        return outcome

    rules = definitions.rules_for(state.trigger_name)
    rule_states = {
        rs.rule_name: rs
        for rs in session.exec(
            select(RuleState).where(
                RuleState.workspace_id == workspace_id,
                RuleState.trigger_name == state.trigger_name,
            )
        ).all()
    }
    active = {
        name: rule
        for name, rule in rules.items()
        if name in rule_states and rule_states[name].enabled
    }
    if not active:
        outcome.skipped = "no enabled rules on this trigger"
        return outcome

    since = (state.watermark or moment) - timedelta(seconds=state.lookback_seconds)
    until = moment

    try:
        # Each trigger reads its own source, which may be Postgres, MySQL or Supabase;
        # the query is checked and rendered in that engine's dialect.
        if dsn is None:
            dsn = store.trigger_dsn(
                session,
                definitions,
                state.trigger_name,
                master_key=master_key,
                workspace_id=workspace_id,
            )
        dialect = sources.DIALECTS[sources.kind_of(dsn)]
        compiled = compile_query(
            trigger, since=since, until=until, limit=definitions.source.max_rows, dialect=dialect
        )
    except (store.StoreError, sources.EngineError) as exc:
        outcome.error = str(exc)[:500]
        return outcome
    except CompileError as exc:
        outcome.error = "; ".join(exc.errors)
        return outcome

    run = Run(
        workspace_id=workspace_id,
        trigger_name=state.trigger_name,
        window_start=since,
        window_end=until,
        status=RunStatus.running,
    )
    session.add(run)
    session.commit()

    try:
        with customer_db.connect(dsn, definitions.source.statement_timeout_ms) as conn:
            rows, _ = conn.fetch(compiled.sql, dict(compiled.params))
    except customer_db.EngineError as exc:
        run.status = RunStatus.failed
        run.error = str(exc)[:500]
        run.finished_at = datetime.now(UTC)
        session.add(run)
        session.commit()
        outcome.error = str(exc)[:500]
        return outcome

    outcome.matched_rows = len(rows)
    per_rule = {name: RuleOutcome(rule_name=name, mode=rule_states[name].mode) for name in active}

    for row in rows:
        if row.get(trigger.event_time) is None:
            outcome.null_event_time_rows += 1
            continue

        for name, rule in active.items():
            claim = ledger.claim(
                session,
                rule_name=name,
                trigger=trigger,
                rule=rule,
                row=row,
                run_id=run.id,
                workspace_id=workspace_id,
            )
            if not claim.fired:
                per_rule[name].fires_suppressed += 1
                continue

            per_rule[name].fires_new += 1
            if deliver is not None:
                sent = deliver(
                    session,
                    workspace=workspace,
                    rule_name=name,
                    mode=rule_states[name].mode,
                    row=row,
                    fire_id=claim.fire_id,
                    master_key=master_key,
                    workspace_id=workspace_id,
                )
                outcome.deliveries.extend(sent if isinstance(sent, list) else [sent])

        # Commit per row, after every rule has had it: a crash mid-run must
        # not roll back fires that were already delivered.
        session.commit()

    outcome.rules = list(per_rule.values())
    run.matched_rows = outcome.matched_rows
    run.fires_new = outcome.fires_new
    run.fires_suppressed = outcome.fires_suppressed
    run.null_event_time_rows = outcome.null_event_time_rows
    run.status = RunStatus.ok
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()

    return outcome


def tick(
    session: Session,
    *,
    master_key: bytes | None = None,
    now: datetime | None = None,
    workspace_id: uuid.UUID | None = DEFAULT_WORKSPACE_ID,
    deliver: Any = None,
) -> PollOutcome | None:
    """Lease one due trigger and poll it. None when nothing is due.

    `workspace_id=None` serves every workspace: the trigger leased decides
    which one this poll acts on.
    """
    state = lease_next(session, workspace_id=workspace_id)
    if state is None:
        return None
    workspace_id = state.workspace_id

    active = store.active_definitions(session, workspace_id=workspace_id)
    if active is None:
        release(session, state, now=now, error="no definitions stored")
        return PollOutcome(trigger_name=state.trigger_name, error="no definitions stored")

    _, definitions = active
    if not store.list_connections(session, workspace_id=workspace_id):
        release(session, state, now=now, error="no connection configured")
        return PollOutcome(trigger_name=state.trigger_name, error="no connection configured")

    window_end = now or datetime.now(UTC)
    try:
        # Resolved per trigger: each one reads the source it names.
        dsn = (
            store.trigger_dsn(
                session,
                definitions,
                state.trigger_name,
                master_key=master_key,
                workspace_id=workspace_id,
            )
            if state.trigger_name in definitions.triggers
            else None
        )
        outcome = poll(
            session,
            state,
            definitions,
            dsn,
            now=window_end,
            workspace_id=workspace_id,
            deliver=deliver,
            master_key=master_key,
        )
    except Exception as exc:  # noqa: BLE001 -- a worker must not die on one trigger
        release(session, state, now=window_end, error=str(exc)[:500])
        return PollOutcome(trigger_name=state.trigger_name, error=str(exc)[:500])

    # The watermark only advances when the poll actually succeeded. A failed
    # run must re-cover its window, not skip it.
    release(
        session,
        state,
        now=window_end,
        advance_watermark_to=window_end if outcome.error is None else None,
        error=outcome.error,
    )
    return outcome


def due_count(session: Session, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID) -> int:
    return len(
        session.exec(
            select(TriggerState).where(
                TriggerState.workspace_id == workspace_id,
                TriggerState.enabled,  # type: ignore[arg-type]
            )
        ).all()
    )


def workspace_of(session: Session, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID) -> Workspace:
    return store.ensure_workspace(session, workspace_id)
