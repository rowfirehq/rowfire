"""The command line, now that the control plane is the only store.

These exist because the file-based commands were replaced wholesale. The
interesting cases are not "does it print a table" but the three places where
removing the file changed behaviour: where definitions come from, where the
customer DSN comes from, and what happens when neither is there yet.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy
from click.testing import CliRunner
from sqlalchemy import text
from sqlmodel import Session

from rowfire.cli import cli
from rowfire.platform import crypto, store
from rowfire.platform.db import PlatformError, check_ready

PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)
FIXTURE_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://rowfire_ro:rowfire_ro@localhost:5433/rowfire_fixture",
)

DEFS = """
version: 2
source: { dsn_env: DATABASE_URL }
triggers:
  order_completed:
    sql: SELECT id, completed_at FROM orders WHERE status = 4
    event_time: completed_at
    key: [id]
rules:
  tell_ops:
    trigger: order_completed
    policy: once_ever
"""


def _available() -> bool:
    try:
        engine = sqlalchemy.create_engine(PLATFORM_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM definition_version LIMIT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="control-plane database unavailable")


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("ROWFIRE_PLATFORM_DSN", PLATFORM_DSN)
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, crypto.generate_master_key())

    from rowfire.platform import db

    db.reset_engine()
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DO $$
                DECLARE t text;
                BEGIN
                  FOR t IN
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                  LOOP
                    EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', t);
                  END LOOP;
                END $$;
                """
            )
        )
    yield engine
    engine.dispose()
    db.reset_engine()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def stored(engine):
    """A control plane with one version of definitions in it."""
    with Session(engine) as session:
        store.save_definitions(session, DEFS)
        session.commit()


# ------------------------------------------------------- nothing stored yet


def test_list_says_what_to_do_when_nothing_is_stored(engine, runner) -> None:
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 1
    # Names both ways out. There is no file to fall back to, so an empty table
    # would read as "you have no triggers" rather than "nothing is set up".
    assert "rowfire init" in result.output
    assert "platform push" in result.output


def test_run_refuses_when_nothing_is_stored(engine, runner) -> None:
    result = runner.invoke(cli, ["run", "tell_ops"])
    assert result.exit_code == 1
    assert "no definitions stored" in result.output


def test_commands_no_longer_accept_a_file(engine, runner) -> None:
    # The flag is gone on purpose: a CLI reading a file while the worker read
    # the database is exactly the disagreement this change removes.
    result = runner.invoke(cli, ["list", "-f", "definitions.yaml"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


# ------------------------------------------------------------ reading the DB


def test_list_reads_the_stored_definitions(stored, runner) -> None:
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "order_completed" in result.output
    assert "tell_ops" in result.output
    # The version is shown, because "which definitions am I looking at" is now
    # a real question with a real answer.
    assert "version 1" in result.output


def test_explain_compiles_a_stored_trigger(stored, runner) -> None:
    result = runner.invoke(cli, ["explain", "order_completed"])
    assert result.exit_code == 0, result.output
    assert "SELECT" in result.output


# ------------------------------------------------------------ import/export


def test_push_if_empty_imports_into_an_empty_plane(engine, runner, tmp_path) -> None:
    path = tmp_path / "definitions.yaml"
    path.write_text(DEFS)

    result = runner.invoke(cli, ["platform", "push", "-f", str(path), "--if-empty"])
    assert result.exit_code == 0, result.output
    assert "version 1" in result.output

    with Session(engine) as session:
        assert store.require_definitions(session)[0] == 1


def test_push_if_empty_is_a_no_op_when_something_exists(stored, runner, tmp_path) -> None:
    # Seeding runs on every `docker compose up`, so "nothing to do" has to be
    # a success. Exiting non-zero here would fail the whole stack on restart.
    path = tmp_path / "definitions.yaml"
    path.write_text(DEFS.replace("once_ever", "once_per_period\n    period: day"))

    result = runner.invoke(cli, ["platform", "push", "-f", str(path), "--if-empty"])
    assert result.exit_code == 0, result.output
    assert "already stored" in result.output

    from rowfire.platform.db import session_scope

    with session_scope() as session:
        version, definitions = store.require_definitions(session)
    assert version == 1, "the seed must not overwrite what is there"
    assert definitions.rules["tell_ops"].policy.value == "once_ever"


def test_push_without_if_empty_still_adds_a_version(stored, runner, tmp_path) -> None:
    path = tmp_path / "definitions.yaml"
    path.write_text(DEFS)
    result = runner.invoke(cli, ["platform", "push", "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert "version 2" in result.output


def test_pull_round_trips_the_exact_text(stored, runner) -> None:
    # Byte-for-byte: the stored document is what was imported, not a
    # re-serialisation of it, so comments and ordering survive the round trip.
    result = runner.invoke(cli, ["platform", "pull"])
    assert result.exit_code == 0, result.output
    assert result.output == DEFS


def test_pull_can_reach_an_older_version(stored, runner, tmp_path) -> None:
    path = tmp_path / "definitions.yaml"
    path.write_text(DEFS.replace("status = 4", "status IN (4, 7)"))
    runner.invoke(cli, ["platform", "push", "-f", str(path)])

    latest = runner.invoke(cli, ["platform", "pull"])
    assert "status IN (4, 7)" in latest.output

    earlier = runner.invoke(cli, ["platform", "pull", "--version", "1"])
    assert "status = 4" in earlier.output
    assert "IN (4, 7)" not in earlier.output


def test_pull_to_a_file_is_an_explicit_export(stored, runner, tmp_path) -> None:
    out = tmp_path / "exported.yaml"
    result = runner.invoke(cli, ["platform", "pull", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text() == DEFS


# ------------------------------------------------------------- where the DSN


def test_the_stored_connection_wins_over_the_environment(engine, monkeypatch) -> None:
    """The CLI and the worker must read the same database.

    A CLI that answered from the env var while the scheduler used the stored
    connection would report on the wrong data while looking perfectly healthy.
    """
    from rowfire.definitions import loads as loads_definitions

    monkeypatch.setenv("DATABASE_URL", "postgresql://env@elsewhere:5432/wrong")
    definitions = loads_definitions(DEFS)

    with Session(engine) as session:
        store.save_connection(session, FIXTURE_DSN)
        session.commit()
        assert store.customer_dsn(session, definitions) == FIXTURE_DSN


def test_the_environment_is_the_fallback_on_a_cold_install(engine, monkeypatch) -> None:
    from rowfire.definitions import loads as loads_definitions

    monkeypatch.setenv("DATABASE_URL", FIXTURE_DSN)
    with Session(engine) as session:
        assert store.customer_dsn(session, loads_definitions(DEFS)) == FIXTURE_DSN


def test_no_connection_anywhere_is_reported_clearly(engine, monkeypatch) -> None:
    from rowfire.definitions import loads as loads_definitions

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with Session(engine) as session:
        with pytest.raises(store.StoreError) as excinfo:
            store.customer_dsn(session, loads_definitions(DEFS))
    assert "DATABASE_URL" in str(excinfo.value)


# ------------------------------------------------------------- readiness


def test_check_ready_passes_against_a_migrated_plane(engine) -> None:
    check_ready(engine)


def test_check_ready_names_an_unmigrated_schema(monkeypatch) -> None:
    # The remedy is `alembic upgrade head`, so the error should say that
    # rather than surfacing a raw ProgrammingError.
    blank = sqlalchemy.create_engine(PLATFORM_DSN.rsplit("/", 1)[0] + "/postgres")
    with pytest.raises(PlatformError, match="not migrated"):
        check_ready(blank)
    blank.dispose()


def test_check_ready_reports_an_unreachable_plane() -> None:
    dead = sqlalchemy.create_engine(
        "postgresql+psycopg://nobody@127.0.0.1:1/nothing", connect_args={"connect_timeout": 2}
    )
    with pytest.raises(PlatformError) as excinfo:
        check_ready(dead)
    assert "unreachable" in str(excinfo.value)
    dead.dispose()
