"""Pushed events: the keys apps send them with, storing what arrives, the
catalog apps report, and how long events are kept.

The in-app SDK (rowfirehq/rowfire-sdk-python) sends three things to the ingest server
(`rowfire ingest`, ingest.py), which hands them here:

  events    stored in `ingested_event`, deduplicated on the SDK's event id,
            and read by triggers through a `rowfire://events` source
  catalog   the latest snapshot per app process, combined for the dashboard
  losses    what an SDK counted as undeliverable, by reason and day

and reads one: the subscriptions in the active definitions, as its config.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from ..definitions import Definitions
from .models import (
    DEFAULT_WORKSPACE_ID,
    CatalogSnapshot,
    IngestedEvent,
    IngestKey,
    SdkLoss,
)

KEY_PREFIX = "rf_"
# Recorded at most this often per key, so authenticating is a read, not a
# write per request.
_LAST_USED_RESOLUTION = timedelta(minutes=1)
# Catalog snapshots older than this are from processes long gone.
CATALOG_HORIZON = timedelta(days=7)


class EventsError(Exception):
    pass


# ------------------------------------------------------------------- keys


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_key(
    session: Session, name: str, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> tuple[IngestKey, str]:
    """A new key. The secret is returned once and never stored."""
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    key = IngestKey(
        workspace_id=workspace_id,
        name=name,
        key_prefix=secret[: len(KEY_PREFIX) + 8],
        key_hash=_hash(secret),
    )
    session.add(key)
    session.flush()
    return key, secret


def list_keys(
    session: Session, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> list[IngestKey]:
    return list(
        session.scalars(
            select(IngestKey)
            .where(IngestKey.workspace_id == workspace_id)
            .order_by(IngestKey.created_at)
        ).all()
    )


def revoke_key(
    session: Session, prefix: str, *, workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
) -> IngestKey:
    key = session.scalars(
        select(IngestKey).where(
            IngestKey.workspace_id == workspace_id,
            IngestKey.key_prefix == prefix,
            IngestKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
    ).first()
    if key is None:
        raise EventsError(f"no active key starting `{prefix}`")
    key.revoked_at = datetime.now(UTC)
    session.add(key)
    return key


def authenticate(session: Session, presented: str) -> IngestKey | None:
    """The active key `presented` belongs to, or None."""
    if not presented.startswith(KEY_PREFIX):
        return None
    key = session.scalars(
        select(IngestKey).where(
            IngestKey.key_hash == _hash(presented),
            IngestKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
    ).first()
    if key is None:
        return None
    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > _LAST_USED_RESOLUTION:
        session.execute(update(IngestKey).where(IngestKey.id == key.id).values(last_used_at=now))
    return key


# ----------------------------------------------------------------- events


def store_events(
    session: Session,
    events: Iterable[Mapping[str, Any]],
    *,
    workspace_id: uuid.UUID,
) -> tuple[int, int]:
    """Store a batch in one statement. Returns (stored, duplicates).

    A duplicate is an event id this workspace already has: the copy a retry
    sends after a timeout that had in fact arrived. It is skipped by the
    unique constraint, not by a read first, so two ingest servers racing the
    same batch still store it once.
    """
    rows = [
        {
            "workspace_id": workspace_id,
            "event_id": event["id"],
            "subscription": event["subscription"],
            "operation": event.get("operation"),
            "occurred_at": event.get("occurred_at"),
            "config_version": event.get("config_version"),
            "data": event.get("data") or {},
        }
        for event in events
    ]
    if not rows:
        return 0, 0
    statement = (
        insert(IngestedEvent.__table__)  # type: ignore[attr-defined]
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_ingested_event_id")
        .returning(IngestedEvent.__table__.c.id)  # type: ignore[attr-defined]
    )
    stored = len(session.execute(statement).all())
    return stored, len(rows) - stored


def record_losses(
    session: Session,
    lost: Mapping[str, int],
    *,
    workspace_id: uuid.UUID,
    day: date | None = None,
) -> None:
    """Add an SDK's loss counts to today's totals."""
    rows = [
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "reason": str(reason)[:100],
            "day": day or datetime.now(UTC).date(),
            "count": int(count),
        }
        for reason, count in lost.items()
        if int(count) > 0
    ]
    if not rows:
        return
    statement = insert(SdkLoss.__table__).values(rows)  # type: ignore[attr-defined]
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_sdk_loss_day",
            set_={"count": SdkLoss.__table__.c["count"] + statement.excluded["count"]},  # type: ignore[attr-defined]
        )
    )


def recent_events(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    subscription: str | None = None,
    limit: int = 50,
) -> list[IngestedEvent]:
    query = select(IngestedEvent).where(IngestedEvent.workspace_id == workspace_id)
    if subscription:
        query = query.where(IngestedEvent.subscription == subscription)
    order = IngestedEvent.__table__.c.id.desc()  # type: ignore[attr-defined]
    return list(session.scalars(query.order_by(order).limit(min(limit, 500))).all())


def losses(session: Session, *, workspace_id: uuid.UUID, days: int = 7) -> list[dict[str, Any]]:
    since = datetime.now(UTC).date() - timedelta(days=days - 1)
    rows = session.scalars(
        select(SdkLoss)
        .where(SdkLoss.workspace_id == workspace_id, SdkLoss.day >= since)
        .order_by(SdkLoss.day, SdkLoss.reason)
    ).all()
    return [{"day": row.day.isoformat(), "reason": row.reason, "count": row.count} for row in rows]


def prune(session: Session, older_than: timedelta, *, batch: int = 10_000) -> int:
    """Delete events received before `older_than` ago, in batches.

    In batches so a large first prune does not hold one long lock on the
    table every poll reads.
    """
    cutoff = datetime.now(UTC) - older_than
    table = IngestedEvent.__table__  # type: ignore[attr-defined]
    total = 0
    while True:
        ids = select(table.c.id).where(table.c.received_at < cutoff).limit(batch)
        deleted = session.execute(delete(table).where(table.c.id.in_(ids))).rowcount or 0
        session.commit()
        total += deleted
        if deleted < batch:
            return total


# ---------------------------------------------------------------- catalog


def save_catalog(session: Session, snapshot: Mapping[str, Any], *, workspace_id: uuid.UUID) -> None:
    """Keep the newest snapshot of one app process, replacing its last one."""
    instance = snapshot.get("instance") or {}
    instance_id = str(instance.get("id") or "")[:64]
    if not instance_id:
        raise EventsError("a catalog needs instance.id")
    now = datetime.now(UTC)
    statement = insert(CatalogSnapshot.__table__).values(  # type: ignore[attr-defined]
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        instance_id=instance_id,
        started_at=_parse_time(instance.get("started_at")),
        received_at=now,
        snapshot=dict(snapshot),
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_catalog_snapshot_instance",
            set_={"snapshot": statement.excluded.snapshot, "received_at": now},
        )
    )
    table = CatalogSnapshot.__table__  # type: ignore[attr-defined]
    session.execute(
        delete(table).where(
            table.c.workspace_id == workspace_id,
            table.c.received_at < now - CATALOG_HORIZON,
        )
    )


def merged_catalog(
    session: Session, *, workspace_id: uuid.UUID, horizon: timedelta = CATALOG_HORIZON
) -> dict[str, Any]:
    """Every recent app process's catalog, added up into one.

    Counts are summed across processes; columns, values and transitions are
    unioned. A column one process saw too many values for stays marked so.
    """
    snapshots = session.scalars(
        select(CatalogSnapshot).where(
            CatalogSnapshot.workspace_id == workspace_id,
            CatalogSnapshot.received_at >= datetime.now(UTC) - horizon,
        )
    ).all()
    merged: dict[str, Any] = {"operations": {}, "tables": {}, "instances": len(snapshots)}
    for row in snapshots:
        _merge_snapshot(merged, row.snapshot)
    return merged


def _merge_snapshot(into: dict[str, Any], snapshot: Mapping[str, Any]) -> None:
    for name, op in (snapshot.get("operations") or {}).items():
        target = into["operations"].setdefault(
            name,
            {
                "declared": False,
                "seen": 0,
                "errors": 0,
                "statuses": {},
                "params": {},
                "actor": {},
                "writes": [],
            },
        )
        target["declared"] = target["declared"] or bool(op.get("declared"))
        target["seen"] += int(op.get("seen") or 0)
        target["errors"] += int(op.get("errors") or 0)
        for status, n in (op.get("statuses") or {}).items():
            target["statuses"][status] = target["statuses"].get(status, 0) + int(n)
        for section in ("params", "actor"):
            for key, values in (op.get(section) or {}).items():
                target[section][key] = _merge_values(target[section].get(key), values)
        for write in op.get("writes") or []:
            existing = next(
                (
                    w
                    for w in target["writes"]
                    if (w["table"], w["kind"]) == (write.get("table"), write.get("kind"))
                ),
                None,
            )
            if existing is None:
                existing = {
                    "table": write.get("table"),
                    "kind": write.get("kind"),
                    "seen": 0,
                    "sampled": 0,
                    "changed": {},
                }
                target["writes"].append(existing)
            existing["seen"] += int(write.get("seen") or 0)
            existing["sampled"] += int(write.get("sampled") or 0)
            for column, n in (write.get("changed") or {}).items():
                existing["changed"][column] = existing["changed"].get(column, 0) + int(n)

    for name, table in (snapshot.get("tables") or {}).items():
        target = into["tables"].setdefault(name, {"columns": {}, "transitions": {}})
        for column, info in (table.get("columns") or {}).items():
            schema = {k: v for k, v in info.items() if k not in ("values", "many_values")}
            current = target["columns"].setdefault(column, schema)
            for key, value in schema.items():
                current.setdefault(key, value)
            observed = _merge_values(
                {k: current[k] for k in ("values", "many_values") if k in current},
                {k: info[k] for k in ("values", "many_values") if k in info},
            )
            current.pop("values", None)
            current.pop("many_values", None)
            current.update(observed)
        for column, moves in (table.get("transitions") or {}).items():
            target["transitions"][column] = _merge_values(
                target["transitions"].get(column), moves, key=("from", "to")
            )


def _merge_values(
    a: Mapping[str, Any] | None,
    b: Mapping[str, Any] | None,
    *,
    key: tuple[str, ...] = ("value",),
) -> dict[str, Any]:
    a = a or {}
    b = b or {}
    if a.get("many_values") or b.get("many_values"):
        return {"many_values": True}
    counts: dict[str, tuple[dict[str, Any], int]] = {}
    for entry in [*(a.get("values") or []), *(b.get("values") or [])]:
        ident = json.dumps([entry.get(k) for k in key], sort_keys=True, default=str)
        seen = counts.get(ident, (entry, 0))[1] + int(entry.get("seen") or 0)
        counts[ident] = (entry, seen)
    if not counts:
        return {}
    return {
        "values": [
            {**{k: entry.get(k) for k in key}, "seen": seen} for entry, seen in counts.values()
        ]
    }


# ----------------------------------------------------------------- config


def sdk_config(definitions: Definitions | None, version: int | None) -> tuple[dict[str, Any], str]:
    """The subscriptions an SDK should match, and an ETag for them."""
    subscriptions = (
        [sub.for_sdk(name) for name, sub in sorted(definitions.subscriptions.items())]
        if definitions is not None
        else []
    )
    config = {
        "version": str(version) if version is not None else None,
        "subscriptions": subscriptions,
    }
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config, f'"{digest[:20]}"'


def count_events(session: Session, *, workspace_id: uuid.UUID) -> int:
    table = IngestedEvent.__table__  # type: ignore[attr-defined]
    return int(
        session.execute(
            select(func.count()).select_from(table).where(table.c.workspace_id == workspace_id)
        ).scalar_one()
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
