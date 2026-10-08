"""Engine tests against the fixture database.

Requires `docker compose up -d postgres`. Skipped, not failed, when it is not
running -- the rest of the suite is pure and should stay runnable anywhere.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from rowfire import engine, introspect
from rowfire.compile import compile_trigger
from rowfire.definitions import DedupPolicy
from rowfire.definitions import load as load_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]

# The fixture database from docker-compose.yml. Host port defaults to 5433
# because 5432 is so often already taken.
FIXTURE_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture",
)

# The names in definitions.yaml, referenced once each so a rename in that file
# fails here loudly instead of in twenty assertions.
NAIVE = "tell_ops_every_completed_order"
INCLUSIVE = "compare_inclusive"
PER_CUSTOMER_WEEKLY = "thank_the_customer_weekly"
PER_CUSTOMER_DAILY = "nudge_ops_daily"


def _database_available() -> bool:
    try:
        with psycopg.connect(FIXTURE_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not _database_available(),
        reason="fixture database unavailable; run `docker compose up -d postgres`",
    ),
]


@pytest.fixture
def conn():
    with engine.connect(FIXTURE_DSN) as connection:
        yield connection


@pytest.fixture
def fixture_definitions(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", FIXTURE_DSN)
    return load_definitions(REPO_ROOT / "definitions.yaml")


# --------------------------------------------------------------- safety


def test_write_is_refused_by_the_database(conn) -> None:
    # Acceptance criterion: a write fails at the database level, not by
    # convention. Either the read-only transaction or the SELECT-only grant
    # is enough on its own; both are in force here.
    with pytest.raises(psycopg.Error) as excinfo:
        conn.raw.execute("INSERT INTO services (name, slug) VALUES ('x', 'x')")
    message = str(excinfo.value).lower()
    assert "read-only" in message or "permission denied" in message
    conn.rollback()


def test_ddl_is_refused_by_the_database(conn) -> None:
    with pytest.raises(psycopg.Error):
        conn.raw.execute("CREATE TABLE should_not_exist (id int)")
    conn.rollback()


def test_statement_timeout_is_set(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()["statement_timeout"] == "30s"
    conn.rollback()


def test_session_is_read_only(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        assert cur.fetchone()["default_transaction_read_only"] == "on"
    conn.rollback()


def test_a_trigger_cannot_write_even_with_arbitrary_sql(conn) -> None:
    # Format 2 lets a trigger be any SELECT, which removes a whole validator.
    # The database-level guarantee is what is left, so it is asserted directly
    # rather than assumed: even a write smuggled through a CTE is refused.
    with pytest.raises(psycopg.Error):
        conn.raw.execute("WITH x AS (DELETE FROM orders RETURNING id) SELECT count(*) FROM x")
    conn.rollback()


def test_missing_env_var_does_not_leak_a_dsn(monkeypatch, fixture_definitions) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(engine.EngineError) as excinfo:
        engine.resolve_dsn(fixture_definitions)
    assert "DATABASE_URL" in str(excinfo.value)
    assert "postgresql://" not in str(excinfo.value).replace(
        "postgresql://readonly@host:5432/dbname", ""
    )


def test_connection_error_does_not_include_the_dsn() -> None:
    secret = "postgresql://user:sup3rsecret@127.0.0.1:1/nope"
    with pytest.raises(engine.EngineError) as excinfo:
        with engine.connect(secret):
            pass
    assert "sup3rsecret" not in str(excinfo.value)


# ---------------------------------------------------------- introspection


def test_read_schema_sees_keys_as_a_select_only_role(conn) -> None:
    # Regression: information_schema.table_constraints hides constraints from
    # a SELECT-only role, so this returned zero primary keys and `init`
    # produced an empty definitions file. pg_catalog has no such filter.
    tables = introspect.read_schema(conn)
    assert {"orders", "customers", "workers", "services"} <= set(tables)
    assert tables["orders"].primary_key == "id"
    assert {fk.target_table for fk in tables["orders"].foreign_keys} == {
        "customers",
        "workers",
        "services",
    }


def test_init_output_validates_with_no_edits(conn, tmp_path, monkeypatch) -> None:
    # Acceptance criterion: the draft is usable as it stands, not a sketch the
    # user has to repair before anything will run.
    monkeypatch.setenv("DATABASE_URL", FIXTURE_DSN)
    tables = introspect.read_schema(conn)
    draft = tmp_path / "definitions.yaml"
    draft.write_text(introspect.draft_yaml(tables))

    definitions = load_definitions(draft)
    assert definitions.triggers, "the draft should start the user with a trigger"

    with engine.connect(FIXTURE_DSN) as verify_conn:
        assert introspect.validate_definitions(definitions, verify_conn) == []


def test_event_time_is_guessed_sensibly(conn) -> None:
    tables = introspect.read_schema(conn)
    assert introspect.guess_event_time(tables["orders"]) == "completed_at"
    assert introspect.guess_event_time(tables["workers"]) == "activated_at"


def test_validate_definitions_catches_a_clock_that_is_not_a_column(
    conn, fixture_definitions
) -> None:
    # event_time names an *output* column of the query. Nothing else checks it,
    # so a typo here would otherwise surface as a SQL error at 3am.
    fixture_definitions.triggers["order_completed"].event_time = "not_a_column"
    errors = introspect.validate_definitions(fixture_definitions, conn)
    assert any("not_a_column" in e for e in errors)


def test_validate_definitions_catches_a_key_that_is_not_a_column(conn, fixture_definitions) -> None:
    fixture_definitions.triggers["order_completed"].key = ["nope"]
    errors = introspect.validate_definitions(fixture_definitions, conn)
    assert any("nope" in e and "key" in e for e in errors)


def test_describe_trigger_reads_columns_from_the_cursor(conn, fixture_definitions) -> None:
    # The composer offers these as choices for the clock and the key, so they
    # have to include aliases and joined columns -- which is exactly what no
    # amount of static analysis would get right.
    columns = introspect.describe_trigger(conn, fixture_definitions.triggers["customer_ordered"])
    assert columns["customer_id"] == "identifier"
    assert columns["completed_at"] == "timestamp"
    assert columns["first_name"] == "string"  # from the joined table
    assert columns["order_id"] == "identifier"  # an alias, not a real column name


# ------------------------------------------------------------- backtest


# No expected count lives here, not even a band.
#
# The seed places rows relative to now(), so a fixed window slides across the
# data and the oldest orders fall out of the back of it. An exact number rots
# within hours; a band rots within days -- it only moves the expiry date, which
# is what happened to the first attempt at this. Assert the properties that do
# not depend on when the fixture was seeded.


def test_run_is_stable_across_repeated_runs(fixture_definitions) -> None:
    # Acceptance criterion: the same window returns the same count. That is a
    # statement about determinism, not about any particular number.
    now = datetime.now(UTC)
    first = engine.run(fixture_definitions, NAIVE, days=120, now=now)
    second = engine.run(fixture_definitions, NAIVE, days=120, now=now)
    assert first.fires == second.fires
    assert first.unique_keys == second.unique_keys
    assert first.fires > 0, "the fixture should always have something in range"


def test_unknown_rule_names_the_known_ones(fixture_definitions) -> None:
    from rowfire.compile import CompileError

    with pytest.raises(CompileError) as excinfo:
        engine.run(fixture_definitions, "no_such_rule")
    assert NAIVE in excinfo.value.errors[0]


def test_null_event_time_rows_are_counted_not_dropped(fixture_definitions) -> None:
    # Acceptance criterion: the 40 planted rows are surfaced separately.
    #
    # This count *is* exact and never drifts, unlike the fire count: the probe
    # carries no time filter, because a row with no event_time cannot be placed
    # in a window at all.
    result = engine.run(fixture_definitions, NAIVE, days=120, sample=500)
    assert result.null_event_time_rows == 40

    # And they are not quietly folded into the headline count -- asserted
    # structurally rather than against a number, so it stays true tomorrow.
    assert result.fires > 0
    assert all(row["completed_at"] is not None for row in result.sample)
    assert result.per_day, "every fire was placed on the timeline"


def test_future_rows_fall_outside_the_window(fixture_definitions) -> None:
    # The five clock-skew rows must not appear, and must not become the
    # busiest day.
    result = engine.run(fixture_definitions, NAIVE, days=120)
    busiest = result.busiest_day()
    assert busiest is not None
    assert busiest[0] <= datetime.now(UTC).date()


def test_auto_completed_orders_are_missed_by_the_naive_query(fixture_definitions) -> None:
    # The soft-break scenario: two status values both mean "done". This is the
    # demo that justifies the whole product, so it is asserted directionally
    # and never against a count.
    now = datetime.now(UTC)
    naive = engine.run(fixture_definitions, NAIVE, days=120, now=now)
    inclusive = engine.run(fixture_definitions, INCLUSIVE, days=120, now=now)
    assert inclusive.fires > naive.fires


def test_test_accounts_are_excluded(fixture_definitions) -> None:
    result = engine.run(fixture_definitions, NAIVE, days=120, sample=1000)
    assert all(row["customer_id"] <= 200 for row in result.sample)


def test_joined_columns_are_available_to_templates(fixture_definitions) -> None:
    # Enrichment is not a separate feature: a joined column is just a column,
    # typed from the cursor like any other.
    result = engine.run(fixture_definitions, PER_CUSTOMER_WEEKLY, days=120, sample=5)
    assert result.column_types["first_name"] == "string"
    assert result.column_types["phone"] == "phone"
    assert all(row.get("first_name") for row in result.sample)


def test_two_rules_on_one_trigger_see_the_same_rows(fixture_definitions) -> None:
    # The fan-out invariant the scheduler depends on: the trigger decides what
    # matched, the rule decides how much of it survives.
    now = datetime.now(UTC)
    weekly = engine.run(fixture_definitions, PER_CUSTOMER_WEEKLY, days=120, now=now)
    daily = engine.run(fixture_definitions, PER_CUSTOMER_DAILY, days=120, now=now)

    assert weekly.trigger_name == daily.trigger_name
    assert weekly.matched_rows == daily.matched_rows
    assert weekly.fires <= daily.fires <= weekly.matched_rows


def test_changing_the_policy_moves_the_count(fixture_definitions) -> None:
    # Acceptance criterion: once_ever vs once_per_period differ in the
    # expected direction. The policy is edited on the *rule* -- the trigger,
    # and therefore the matched rows, are untouched.
    now = datetime.now(UTC)
    weekly = engine.run(fixture_definitions, PER_CUSTOMER_WEEKLY, days=120, now=now)

    rule = fixture_definitions.rules[PER_CUSTOMER_WEEKLY]
    rule.policy = DedupPolicy.once_ever
    rule.period = None
    ever = engine.run(fixture_definitions, PER_CUSTOMER_WEEKLY, days=120, now=now)

    assert ever.fires < weekly.fires
    assert ever.matched_rows == weekly.matched_rows  # same query, different dedup


def test_zero_volume_window_does_not_crash(fixture_definitions) -> None:
    # Acceptance criterion: empty result sets render cleanly rather than
    # dividing by zero. The fixture has a 29-day hole; aim at it.
    result = engine.run(
        fixture_definitions,
        NAIVE,
        days=10,
        now=datetime.now(UTC) - timedelta(days=40),
    )
    assert result.fires == 0
    assert result.mean_per_day == 0.0
    assert result.sample == []
    assert result.busiest_day() is None


def test_sample_spans_the_window(fixture_definitions) -> None:
    result = engine.run(fixture_definitions, NAIVE, days=120, sample=20)
    assert len(result.sample) == 20
    stamps = [r["completed_at"] for r in result.sample]
    # A naive `rows[:20]` would put every sample on the first day.
    assert (max(stamps) - min(stamps)).days > 60


def test_explain_sql_actually_runs(fixture_definitions) -> None:
    # Acceptance criterion: explain output is valid SQL, pasteable into psql.
    # Compared against the engine's own answer for the same window rather than
    # a literal, so the two can never drift apart silently.
    until = datetime.now(UTC)
    compiled = compile_trigger(
        fixture_definitions,
        "order_completed",
        since=until - timedelta(days=120),
        until=until,
    )
    expected = engine.run(fixture_definitions, NAIVE, days=120, now=until)

    with engine.connect(FIXTURE_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(compiled.display_sql())
            assert len(cur.fetchall()) == expected.matched_rows
        conn.rollback()


def test_a_query_that_binds_its_own_window_agrees_with_the_wrapper(
    fixture_definitions,
) -> None:
    # The escape hatch exists for index use, so the two forms must produce the
    # same rows -- otherwise it is a correctness fork dressed as an
    # optimisation.
    from rowfire.definitions import Trigger

    inner = (
        "SELECT id, customer_id, completed_at FROM orders "
        "WHERE status = 4 AND paid_at IS NOT NULL AND is_test = false"
    )
    wrapped = Trigger(sql=inner, event_time="completed_at", key=["id"])
    self_bound = Trigger(
        sql=f"{inner} AND completed_at >= :since AND completed_at < :until",
        event_time="completed_at",
        key=["id"],
    )

    fixture_definitions.triggers["probe_wrapped"] = wrapped
    fixture_definitions.triggers["probe_self_bound"] = self_bound
    fixture_definitions.rules["probe_wrapped"] = fixture_definitions.rules[NAIVE].model_copy(
        update={"trigger": "probe_wrapped"}
    )
    fixture_definitions.rules["probe_self_bound"] = fixture_definitions.rules[NAIVE].model_copy(
        update={"trigger": "probe_self_bound"}
    )

    now = datetime.now(UTC)
    a = engine.run(fixture_definitions, "probe_wrapped", days=30, now=now)
    b = engine.run(fixture_definitions, "probe_self_bound", days=30, now=now)
    assert a.matched_rows == b.matched_rows
    assert a.fires == b.fires


def test_timeout_is_configurable(fixture_definitions) -> None:
    fixture_definitions.source.statement_timeout_ms = 1
    with pytest.raises(engine.EngineError, match="statement_timeout"):
        with engine.connect(FIXTURE_DSN, 1) as conn:
            engine._fetch(
                conn,
                type("Q", (), {"sql": "SELECT pg_sleep(2)", "params": {}})(),
            )


def test_row_cap_is_enforced(fixture_definitions) -> None:
    # A mis-written join must not pull a table into memory.
    fixture_definitions.source.max_rows = 5
    result = engine.run(fixture_definitions, NAIVE, days=120)
    assert result.matched_rows == 5


def test_dsn_is_read_from_the_named_env_var(monkeypatch, fixture_definitions) -> None:
    monkeypatch.setenv("SOMETHING_ELSE", FIXTURE_DSN)
    fixture_definitions.source.dsn_env = "SOMETHING_ELSE"
    assert engine.resolve_dsn(fixture_definitions) == FIXTURE_DSN


def test_environment_is_not_mutated_by_a_run(fixture_definitions) -> None:
    before = dict(os.environ)
    engine.run(fixture_definitions, NAIVE, days=5)
    assert dict(os.environ) == before


def test_a_literal_percent_sign_survives_the_window_parameters(fixture_definitions) -> None:
    # Regression: the window is bound with %(since)s, which made a bare `%`
    # in the author's query a formatting directive and failed the run.
    from rowfire.definitions import Trigger

    fixture_definitions.triggers["probe_like"] = Trigger(
        sql="SELECT id, first_name, created_at FROM customers WHERE first_name LIKE 'Customer1%'",
        event_time="created_at",
        key=["id"],
    )
    fixture_definitions.rules["probe_like"] = fixture_definitions.rules[NAIVE].model_copy(
        update={"trigger": "probe_like"}
    )
    result = engine.run(fixture_definitions, "probe_like", days=3650)
    # Customer1, Customer10-19, Customer100-199: 1 + 10 + 100.
    assert result.matched_rows == 111
