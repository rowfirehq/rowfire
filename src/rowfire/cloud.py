"""Running RowFire on a managed cloud platform (Render, for one).

A platform hands an app a database URL, a port and its public host name, and
not much else. These commands turn that into everything RowFire needs, so the
platform's own config (render.yaml) stays a list of services:

    rowfire cloud predeploy   create schemas, migrate, seed the demo's data
    rowfire cloud serve       the web app, on $PORT
    rowfire cloud worker      the poller

One Postgres database holds everything, a schema each:

    rowfire_platform   the control plane -- workspaces, rules, history
    sample             the demo's sample company, reseeded on every deploy
    visitor_*          each demo visitor's own copy of it (see hosted.py)

`ROWFIRE_MODE` says which kind of instance this is, and must be set:

    demo      the public demo: a private workspace per visitor, sample data,
              actions delivered to the Demo inbox only
    private   one team's own RowFire. Refused for now: on a cloud platform it
              has a public address, and RowFire has no sign-in yet, so anyone
              who found it could connect databases and send messages.

Anything already set in the environment wins over what is derived here, so a
deploy can override a single setting without forking this file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import click

PLATFORM_SCHEMA = "rowfire_platform"
SAMPLE_SCHEMA = "sample"
READER_ROLE = "rowfire_demo_reader"
MODES = ("demo", "private")


class CloudError(Exception):
    """A deploy that cannot work as configured. Never carries a credential."""


def _with_query(url: str, scheme: str | None = None, **params: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in params]
    query.extend(params.items())
    return urlunsplit(parts._replace(scheme=scheme or parts.scheme, query=urlencode(query)))


def _plain(url: str) -> str:
    """postgres:// and postgresql+psycopg:// as the plain postgresql:// psycopg takes."""
    scheme = urlsplit(url).scheme
    if scheme in {"postgres", "postgresql+psycopg"}:
        return url.replace(f"{scheme}://", "postgresql://", 1)
    return url


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


def _master_key(raw: str) -> str:
    """The master key as RowFire reads it: url-safe base64 of 32 bytes.

    A platform-generated secret is random but not necessarily that shape, so
    anything else is hashed down to 32 bytes. Deterministic, so every
    service on the deploy derives the same key from the same secret.
    """
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except ValueError:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()).decode()


def _reader_password(master_key: str) -> str:
    """The demo's read-only role's password, derived: one less secret to keep."""
    return hmac.new(master_key.encode(), b"rowfire-demo-reader", hashlib.sha256).hexdigest()


def _examples_dir() -> Path:
    """examples/saas: copied into the image at /app, or the source tree's."""
    configured = os.environ.get("ROWFIRE_EXAMPLES_DIR")
    if configured:
        return Path(configured)
    for candidate in (
        Path("/app/examples/saas"),
        Path(__file__).resolve().parents[2] / "examples/saas",
    ):
        if candidate.is_dir():
            return candidate
    raise CloudError("cannot find examples/saas; set ROWFIRE_EXAMPLES_DIR")


def settings(env: dict[str, str] | None = None) -> dict[str, str]:
    """What RowFire's own settings should be, derived from the platform's."""
    env = dict(os.environ if env is None else env)
    database = env.get("DATABASE_URL", "")
    if not database:
        raise CloudError("DATABASE_URL is not set; attach a Postgres database to this service")
    mode = env.get("ROWFIRE_MODE", "")
    if mode not in MODES:
        raise CloudError(f"ROWFIRE_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    if mode == "private":
        raise CloudError(
            "ROWFIRE_MODE=private is not available yet. On a cloud platform RowFire has a "
            "public address, and it has no sign-in yet, so anyone who found it could "
            "connect databases and send messages. Run it inside your own network instead "
            "(see the README), or use ROWFIRE_MODE=demo."
        )
    raw_key = env.get("ROWFIRE_MASTER_KEY", "")
    if not raw_key:
        raise CloudError("ROWFIRE_MASTER_KEY is not set")

    master_key = _master_key(raw_key)
    plain = _plain(database)
    # The platform's own address, and a custom domain if one is attached.
    named = ",".join(
        (env.get("RENDER_EXTERNAL_HOSTNAME", ""), env.get("ROWFIRE_PUBLIC_HOSTNAME", ""))
    )
    hosts = [host.strip() for host in named.split(",") if host.strip()]
    examples = _examples_dir()

    derived = {
        "ROWFIRE_MASTER_KEY": master_key,
        "ROWFIRE_PLATFORM_DSN": _with_query(
            plain, scheme="postgresql+psycopg", options=f"-csearch_path={PLATFORM_SCHEMA}"
        ),
        "ROWFIRE_ALLOWED_HOSTS": ",".join(hosts),
        "ROWFIRE_TRUST_PROXY": "1",
        # demo mode
        "ROWFIRE_HOSTED": "1",
        "ROWFIRE_EGRESS": "inbox-only",
        "ROWFIRE_DEMO_TEMPLATE_SCHEMA": SAMPLE_SCHEMA,
        "ROWFIRE_DEMO_DSN": _with_user(plain, READER_ROLE, _reader_password(master_key)),
        "ROWFIRE_DEMO_ACTIVITY_DSN": plain,
        "ROWFIRE_DEMO_ACTIVITY_SQL": str(examples / "activity.sql"),
        "ROWFIRE_HOSTED_DEFINITIONS": str(examples / "definitions.yaml"),
    }
    # The master key is always the normalised one: a raw platform secret
    # would not decode, and every service has to agree on it.
    out = {key: env.get(key) or value for key, value in derived.items()}
    out["ROWFIRE_MASTER_KEY"] = master_key
    return out


def _apply() -> dict[str, str]:
    try:
        values = settings()
    except CloudError as exc:
        click.echo(f"rowfire cloud: {exc}", err=True)
        sys.exit(2)
    os.environ.update(values)
    return values


# ------------------------------------------------------------ predeploy


def _run_sql_file(conn: object, path: Path) -> None:
    conn.execute(path.read_bytes())  # type: ignore[attr-defined]


def prepare_database(values: dict[str, str]) -> None:
    """Schemas, the demo's read-only role, and a freshly seeded sample.

    The sample is dropped and reseeded on every deploy: its timestamps are
    relative to when it was seeded, and a backtest over the last 90 days of a
    sample seeded months ago finds nothing. Visitors' copies own their
    sequences (hosted.copy_sample), so dropping the template leaves them be.
    """
    import psycopg
    from psycopg import sql

    admin = values["ROWFIRE_DEMO_ACTIVITY_DSN"]
    examples = Path(values["ROWFIRE_DEMO_ACTIVITY_SQL"]).parent
    password = urlsplit(values["ROWFIRE_DEMO_DSN"]).password or ""

    with psycopg.connect(admin, connect_timeout=10) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(PLATFORM_SCHEMA))
        )

        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (READER_ROLE,)
        ).fetchone()
        verb = "ALTER" if exists else "CREATE"
        try:
            conn.execute(
                sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.SQL(verb), sql.Identifier(READER_ROLE), sql.Literal(password)
                )
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            raise CloudError(
                "the database user cannot create roles, and the demo needs a read-only "
                "one for visitors' queries. Grant it CREATEROLE, or create "
                f"`{READER_ROLE}` by hand with the password `rowfire cloud reader-password` prints."
            ) from exc
        database = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(READER_ROLE)
            )
        )

        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SAMPLE_SCHEMA))
        )
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SAMPLE_SCHEMA)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SAMPLE_SCHEMA)))
        for name in ("01_schema.sql", "02_seed.sql"):
            _run_sql_file(conn, examples / "db" / name)


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    for root in (Path("/app"), Path(__file__).resolve().parents[2]):
        if (root / "alembic.ini").is_file():
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            command.upgrade(config, "head")
            return
    raise CloudError("cannot find alembic.ini to migrate with")


# ------------------------------------------------------------- commands


@click.group()
def cloud() -> None:
    """Run on a managed cloud platform (see render.yaml and docs/hosting.md)."""


@cloud.command()
def predeploy() -> None:
    """Create schemas, migrate the control plane, and reseed the demo's sample."""
    values = _apply()
    try:
        prepare_database(values)
        _migrate()
    except CloudError as exc:
        click.echo(f"rowfire cloud: {exc}", err=True)
        sys.exit(2)
    click.echo("rowfire cloud: database ready")


@cloud.command()
def serve() -> None:
    """The web app, on the platform's $PORT."""
    _apply()
    port = os.environ.get("PORT", "8000")
    os.execvp(
        "rowfire",
        ["rowfire", "serve", "--host", "0.0.0.0", "--port", port, "--allow-non-loopback"],
    )


@cloud.command()
def worker() -> None:
    """The poller, which also deletes idle demo workspaces."""
    _apply()
    os.execvp("rowfire", ["rowfire", "worker", "--interval", "3"])


@cloud.command(name="reader-password")
def reader_password() -> None:
    """Print the demo's read-only role's password, for creating it by hand."""
    values = _apply()
    click.echo(urlsplit(values["ROWFIRE_DEMO_DSN"]).password or "")
