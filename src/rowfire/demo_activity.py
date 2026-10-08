"""Demo only: add a burst of activity to the *sample* database.

Rowfire never writes to a database it reads, and nothing here changes that.
This is the "Simulate new activity" button in Get started, and it exists only
when a deployment configures it with two settings:

    ROWFIRE_DEMO_ACTIVITY_DSN   a write-capable login to the sample database
    ROWFIRE_DEMO_ACTIVITY_SQL   the path of a fixed SQL file to run against it

Three properties keep it from being a write path into anything real:

  * it is off unless both are set, and nothing sets them outside the demo
    compose files -- a real install has no button and the endpoint is a 404
  * the login is its own setting, never a stored data source, so the sources
    stay read-only and a source's credential can never be used to write
  * it runs that one file and nothing else -- no SQL ever comes from the
    browser

Postgres only, because the sample database it exists for is Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

DSN_ENV = "ROWFIRE_DEMO_ACTIVITY_DSN"
SQL_ENV = "ROWFIRE_DEMO_ACTIVITY_SQL"

# Generous for a handful of inserts, short enough that a wedged sample
# database does not hold a request open.
TIMEOUT_MS = 10_000


class ActivityError(Exception):
    """The script could not be run. Never carries the DSN."""


def configured() -> bool:
    return bool(os.environ.get(DSN_ENV) and os.environ.get(SQL_ENV))


def run(schema: str | None = None) -> None:
    """Run the configured script in one transaction.

    `schema` is where its unqualified table names resolve: a hosted demo
    visitor's own copy of the sample tables, so their activity fires their
    rules and nobody else's. It is never a visitor's input -- hosted derives
    it from the workspace id and checks its shape.
    """
    if not configured():
        raise ActivityError("simulated activity is not configured")

    path = Path(os.environ[SQL_ENV])
    try:
        script = path.read_text()
    except OSError as exc:
        raise ActivityError(f"cannot read {path.name}: {exc.strerror}") from exc

    import psycopg

    dsn = os.environ[DSN_ENV].replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {TIMEOUT_MS}")
            if schema is not None:
                conn.execute("SELECT set_config('search_path', %s, false)", (schema,))
            # One script, several statements: psycopg sends a query with no
            # parameters as a simple query, which may hold more than one.
            conn.execute(script.encode())
    except psycopg.Error as exc:
        # The first line of a Postgres error is the message; the rest can
        # echo connection details.
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ActivityError(f"the sample database refused it: {message}") from exc
