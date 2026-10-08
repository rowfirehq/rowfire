"""Dedup policies. Pure functions, no database.

The split being tested here is the design decision of format 2: the **key**
comes from the trigger -- "one row per order, identified by id" is a fact about
the query -- and the **policy** comes from the rule, because the same query
wants different cadences for different actions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rowfire.dedup import apply, period_bucket
from rowfire.definitions import DedupPolicy, Rule, Trigger

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # a Monday

SQL = "SELECT id, customer_id, completed_at FROM orders"


def trigger(*key: str) -> Trigger:
    return Trigger(sql=SQL, event_time="completed_at", key=list(key))


def rule(policy: DedupPolicy, **kwargs) -> Rule:
    return Rule(trigger="t", policy=policy, **kwargs)


def row(order_id: int, customer_id: int, day_offset: int) -> dict:
    return {
        "id": order_id,
        "customer_id": customer_id,
        "completed_at": BASE + timedelta(days=day_offset),
    }


def test_once_ever_keeps_one_row_per_key() -> None:
    rows = [row(1, 10, 0), row(1, 10, 5), row(2, 10, 1)]
    kept = apply(rows, trigger("id"), rule(DedupPolicy.once_ever))
    assert [r["id"] for r in kept] == [1, 2]


def test_once_ever_keeps_the_earliest_row() -> None:
    # Input deliberately out of order: the policy must not depend on the
    # order the database happened to return rows in.
    rows = [row(1, 10, 9), row(1, 10, 2), row(1, 10, 5)]
    kept = apply(rows, trigger("id"), rule(DedupPolicy.once_ever))
    assert len(kept) == 1
    assert kept[0]["completed_at"] == BASE + timedelta(days=2)


def test_the_key_comes_from_the_trigger_not_the_rule() -> None:
    # Same rule, two triggers at different grains. This is why the key lives
    # where it does: whoever wrote the query knows where duplication happens.
    rows = [row(i, 10, i % 7) for i in range(40)]
    one_rule = rule(DedupPolicy.once_ever)
    assert len(apply(rows, trigger("id"), one_rule)) == 40
    assert len(apply(rows, trigger("customer_id"), one_rule)) == 1


def test_one_trigger_two_rules_at_different_cadences() -> None:
    # The fan-out case the scheduler relies on: one query, several answers.
    rows = [row(i, 10, i) for i in range(21)]  # three weeks, one order a day
    grain = trigger("customer_id")
    weekly = apply(rows, grain, rule(DedupPolicy.once_per_period, period="week"))
    daily = apply(rows, grain, rule(DedupPolicy.once_per_period, period="day"))
    assert len(weekly) == 3
    assert len(daily) == 21


def test_once_per_period_is_never_fewer_than_once_ever() -> None:
    # The direction is what is asserted, not a magic number that would need
    # updating whenever the fixture changes.
    rows = [row(i, 10, i) for i in range(30)]
    grain = trigger("customer_id")
    ever = apply(rows, grain, rule(DedupPolicy.once_ever))
    weekly = apply(rows, grain, rule(DedupPolicy.once_per_period, period="week"))
    daily = apply(rows, grain, rule(DedupPolicy.once_per_period, period="day"))
    assert len(ever) <= len(weekly) <= len(daily) <= len(rows)


def test_once_per_period_drops_rows_with_no_event_time() -> None:
    # They cannot be bucketed. engine.py counts and reports them separately;
    # what matters here is that they do not silently become a fire.
    rows = [row(1, 10, 0), {"id": 2, "customer_id": 10, "completed_at": None}]
    kept = apply(rows, trigger("customer_id"), rule(DedupPolicy.once_per_period, period="day"))
    assert len(kept) == 1


def test_once_per_n_fires_on_every_nth() -> None:
    rows = [row(i, 10, i) for i in range(10)]
    kept = apply(rows, trigger("customer_id"), rule(DedupPolicy.once_per_n, n=3))
    # The 1st, 4th, 7th and 10th occurrence.
    assert [r["id"] for r in kept] == [0, 3, 6, 9]


def test_once_per_n_counts_each_key_independently() -> None:
    rows = [row(i, 10 + (i % 2), i) for i in range(8)]
    kept = apply(rows, trigger("customer_id"), rule(DedupPolicy.once_per_n, n=2))
    assert len(kept) == 4  # two keys, four occurrences each, every other one


def test_composite_key() -> None:
    rows = [row(1, 10, 0), row(1, 11, 1), row(2, 10, 2)]
    kept = apply(rows, trigger("id", "customer_id"), rule(DedupPolicy.once_ever))
    assert len(kept) == 3


def test_empty_input_is_not_an_error() -> None:
    assert apply([], trigger("id"), rule(DedupPolicy.once_ever)) == []


def test_nulls_sort_last_rather_than_raising() -> None:
    rows = [{"id": 1, "completed_at": None}, {"id": 2, "completed_at": BASE}]
    kept = apply(rows, trigger("id"), rule(DedupPolicy.once_ever))
    assert [r["id"] for r in kept] == [2, 1]


def test_a_trigger_with_no_clock_still_dedups() -> None:
    # No event_time means no ordering and no buckets, but once_ever is still
    # answerable -- it is pure uniqueness.
    timeless = Trigger(sql=SQL, key=["id"])
    rows = [{"id": 1}, {"id": 1}, {"id": 2}]
    assert len(apply(rows, timeless, rule(DedupPolicy.once_ever))) == 2


@pytest.mark.parametrize(
    "period,expected",
    [
        ("day", "2026-03-04"),
        ("week", "2026-03-02"),  # Monday, matching date_trunc('week', ...)
        ("month", "2026-03-01"),
    ],
)
def test_period_bucket(period: str, expected: str) -> None:
    when = datetime(2026, 3, 4, 15, 30, tzinfo=UTC)
    assert period_bucket(when, period).isoformat() == expected


def test_week_buckets_start_on_monday_like_postgres() -> None:
    sunday = datetime(2026, 3, 8, 23, 59, tzinfo=UTC)
    monday = datetime(2026, 3, 9, 0, 1, tzinfo=UTC)
    assert period_bucket(sunday, "week") != period_bucket(monday, "week")
