from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from rowfire_sdk._matching import Ceiling, Facts, Matcher, evaluate, plain

REFUND = {
    "id": "goodwill_refund",
    "operation": "POST /admin/charges/{id}/refund",
    "write": {"table": "charges", "kind": "update", "changed": "status"},
    "where": {"row.status": "refunded", "actor.role": "support"},
    "fields": ["row.id", "row.amount", "actor.id"],
}


def test_an_operation_nobody_listens_to_resolves_to_nothing():
    matcher = Matcher([REFUND], Ceiling())
    assert not matcher.resolve("GET /health")
    assert matcher.resolve("POST /admin/charges/{id}/refund").tables["charges"][0].id == (
        "goodwill_refund"
    )


def test_resolution_is_cached_per_operation_name():
    matcher = Matcher([REFUND], Ceiling())
    assert matcher.resolve("GET /health") is matcher.resolve("GET /health")


def test_glob_operations_match_by_pattern():
    sub = {"id": "any_admin", "operation": "POST /admin/*", "fields": ["operation"]}
    matcher = Matcher([sub], Ceiling())
    assert matcher.resolve("POST /admin/charges/{id}/refund").on_complete
    assert not matcher.resolve("POST /checkout")


def test_ceiling_blocks_operations_outside_the_allow_list():
    sub = {"id": "any", "operation": "*", "fields": ["operation"]}
    matcher = Matcher([sub], Ceiling(allow_operations=["POST /checkout"]))
    assert matcher.resolve("POST /checkout")
    assert not matcher.resolve("POST /admin/users/{id}/impersonate")


def test_a_subscription_asking_for_a_denied_field_is_rejected():
    asks = {**REFUND, "id": "asks", "fields": ["row.id", "row.customer_email"]}
    matcher = Matcher([asks], Ceiling(deny_fields=["*email*"]))
    assert not matcher.resolve(REFUND["operation"])
    assert matcher.rejected == [("asks", "denied fields: row.customer_email")]


def test_a_subscription_testing_a_denied_field_is_rejected_too():
    # Firing only for @bigcorp.com addresses would leak something about the email.
    tests = {**REFUND, "id": "tests", "where": {"row.email": "x@bigcorp.com"}}
    matcher = Matcher([tests], Ceiling(deny_fields=["email"]))
    assert matcher.rejected[0][0] == "tests"


def test_malformed_subscriptions_are_rejected_without_breaking_the_rest():
    bad = [
        {"operation": "x"},
        {"id": "unknown_ns", "operation": "x", "fields": ["request.body"]},
        {"id": "row_without_table", "operation": "x", "fields": ["row.id"]},
        {"id": "bad_kind", "operation": "x", "write": {"table": "t", "kind": "upsert"}},
    ]
    matcher = Matcher([*bad, REFUND], Ceiling())
    assert len(matcher.rejected) == 4
    assert matcher.resolve(REFUND["operation"])


def test_evaluate_checks_conditions_and_copies_only_the_named_fields():
    matcher = Matcher([REFUND], Ceiling())
    sub = matcher.resolve(REFUND["operation"]).tables["charges"][0]
    row = {"id": 7, "amount": Decimal("12.50"), "status": "refunded", "secret": "x"}.get
    facts = Facts(REFUND["operation"], {"id": "u1", "role": "support"}, {}, row=row)
    assert evaluate(sub, facts) == {"row.id": 7, "row.amount": 12.5, "actor.id": "u1"}

    as_admin = Facts(REFUND["operation"], {"id": "u1", "role": "admin"}, {}, row=row)
    assert evaluate(sub, as_admin) is None


def test_a_list_in_where_means_any_of():
    sub = {"id": "s", "operation": "x", "where": {"params.plan": ["team", "enterprise"]}}
    resolved = Matcher([sub], Ceiling()).resolve("x").on_complete[0]
    assert evaluate(resolved, Facts("x", {}, {"plan": "team"})) == {}
    assert evaluate(resolved, Facts("x", {}, {"plan": "free"})) is None


def test_plain_makes_values_json_ready():
    class Plan(Enum):
        TEAM = "team"

    when = datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert plain(when) == "2026-10-16T00:00:00+00:00"
    assert plain(Plan.TEAM) == "team"
    # Decimals become numbers, so the database's 49.00 and the app's 49.0 match,
    # unless a float would lose digits.
    assert plain(Decimal("1.10")) == 1.1
    assert plain(Decimal("49.00")) == 49
    assert plain(Decimal("0.1000000000000000055511151231257827")) == (
        "0.1000000000000000055511151231257827"
    )
    assert plain(Decimal("NaN")) == "NaN"
    assert plain(object()).startswith("<object")


def test_on_is_still_accepted_for_write():
    legacy = {**REFUND, "id": "legacy"}
    legacy["on"] = legacy.pop("write")
    assert Matcher([legacy], Ceiling()).resolve(REFUND["operation"]).tables["charges"]
