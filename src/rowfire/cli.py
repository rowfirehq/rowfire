"""Click commands. All terminal output for the non-`run` paths lives here.

Every command reads the definitions in force from the control plane. There is
no file to point at and no `--file` flag: the store of record is the database,
so the CLI, the UI and the worker cannot disagree about what is defined. A
definitions file is an import/export artifact now, handled explicitly by
`rowfire platform push` and `rowfire platform pull`.

Errors are reported in full rather than one per invocation -- a definitions
set with six problems should take one run to diagnose, not six.
"""

from __future__ import annotations

import sys
from datetime import UTC

import click
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__, engine, introspect, report, sources
from .compile import CompileError, compile_trigger
from .definitions import Definitions

console = Console()
err_console = Console(stderr=True)


def _fail(title: str, errors: list[str]) -> None:
    err_console.print(f"\n[bold red]{title}[/bold red]")
    for error in errors:
        err_console.print(f"  [red]•[/red] {error}")
    err_console.print()
    sys.exit(1)


def _active() -> tuple[int, Definitions]:
    """The definitions in force, with their version number.

    The version is read out *inside* the session scope on purpose: ORM
    instances expire on commit, so touching one after the scope closes raises
    DetachedInstanceError.
    """
    from .platform import store
    from .platform.db import PlatformError, session_scope

    try:
        with session_scope() as session:
            return store.require_definitions(session)
    except store.StoreError as exc:
        _fail("nothing to run", [str(exc)])
    except PlatformError as exc:
        _fail("cannot reach the control plane", [str(exc)])
    raise AssertionError("unreachable")  # keeps type checkers happy


def _dsn(definitions: Definitions, source: str | None = None) -> str:
    """One customer database, from a stored source or the environment.

    `source` is a trigger's `source:`; None means the default source.
    """
    from .platform import crypto, store
    from .platform.db import PlatformError, session_scope

    try:
        with session_scope() as session:
            return store.customer_dsn(session, definitions, source=source)
    except (store.StoreError, crypto.CryptoError) as exc:
        _fail("no database to read", [str(exc)])
    except PlatformError as exc:
        _fail("cannot reach the control plane", [str(exc)])
    raise AssertionError("unreachable")


def _dialect_for(definitions: Definitions, source: str | None) -> str:
    """The SQL dialect of a source, without failing when it cannot be found."""
    from .platform import store
    from .platform.db import session_scope

    try:
        with session_scope() as session:
            dsn = store.customer_dsn(session, definitions, source=source)
        return sources.DIALECTS[sources.kind_of(dsn)]
    except Exception:  # noqa: BLE001 -- explain must work offline
        return sources.DIALECTS["postgres"]


@click.group()
@click.version_option(__version__, prog_name="rowfire")
def cli() -> None:
    """Backtest business-event rules against read-only Postgres and MySQL databases.

    Nothing here ever writes to the database you are reading. Connections are
    opened read-only at the session level and every query is a SELECT.

    Definitions live in the control plane, not in a file.
    """


@cli.command()
@click.option(
    "--schema",
    default=None,
    help="Schema to read. Defaults to `public` on Postgres, the connected database on MySQL.",
)
@click.option(
    "--source",
    default=None,
    help="Stored data source to read, when --dsn-env is not set. Defaults to the default source.",
)
@click.option(
    "--dsn-env",
    default="DATABASE_URL",
    show_default=True,
    help="Env var holding the read-only DSN, when none is stored yet.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be stored instead of storing it.",
)
@click.option("--force", is_flag=True, help="Store even though definitions already exist.")
def init(schema: str | None, source: str | None, dsn_env: str, dry_run: bool, force: bool) -> None:
    """Introspect the database and store a starting set of definitions."""
    import os

    from .platform import store
    from .platform.db import PlatformError, session_scope

    dsn = os.environ.get(dsn_env)
    if not dsn:
        # The stored connection is tried first, so this only fires on a truly
        # cold install where neither exists.
        try:
            with session_scope() as session:
                name = source or store.default_source(session)
                connection = store.get_connection(session, name=name) if name else None
                dsn = store.reveal_dsn(connection) if connection else None
        except Exception:  # noqa: BLE001 -- reported as the missing-DSN case
            dsn = None
    if not dsn:
        _fail(
            f"no database connection stored, and {dsn_env} is not set",
            [
                "Export a read-only DSN there, for example:",
                f"  export {dsn_env}='postgresql://readonly@host:5432/dbname'",
                f"  export {dsn_env}='mysql://readonly@host:3306/dbname'",
                "or store one with `rowfire platform connect`.",
            ],
        )

    try:
        with engine.connect(dsn) as conn:
            schema = schema or conn.default_schema()
            tables = introspect.read_schema(conn, schema)
            kind = conn.kind
    except engine.EngineError as exc:
        _fail("could not read the schema", [str(exc)])

    if not tables:
        _fail(f"no tables found in schema `{schema}`", ["Is --schema correct?"])

    text = introspect.draft_yaml(tables, dsn_env=dsn_env, kind=kind)

    if dry_run:
        console.print()
        console.print(Syntax(text, "yaml", theme="ansi_dark", word_wrap=True))
        console.print("\n[dim]--dry-run: nothing was stored.[/dim]\n")
        return

    try:
        with session_scope() as session:
            existing = store.active_definitions(session)
            if existing is not None and not force:
                _fail(
                    f"definitions already exist (version {existing[0].version})",
                    [
                        "Pass --force to store this draft as a new version, or "
                        "`rowfire platform pull` to see what is there.",
                    ],
                )
            version = store.save_definitions(session, text, created_by="init")
            number = version.version
    except PlatformError as exc:
        _fail("could not store the definitions", [str(exc)])

    drafted = len(introspect._draft_triggers(tables))
    skipped = len(tables) - drafted
    console.print(f"\n[green]✓[/green] stored version [bold]{number}[/bold]")
    console.print(f"  {drafted} triggers from {len(tables)} tables", style="dim")
    if skipped:
        console.print(
            f"  {skipped} skipped for having no single-column primary key or no timestamp",
            style="dim",
        )
    console.print(
        "\n[dim]Each trigger runs as written, so `validate` and `run` work now — but\n"
        "none has a WHERE clause, so each one matches every row. No condition is\n"
        "guessed on purpose: `status = 3` would look right and fire wrong.[/dim]\n"
    )


@cli.command()
def validate() -> None:
    """Check the stored definitions against the live schema."""
    version, definitions = _active()

    # One connection per data source, each checking the triggers that read it.
    by_source: dict[str | None, list[str]] = {}
    for name, trigger in definitions.triggers.items():
        by_source.setdefault(trigger.source, []).append(name)

    errors: list[str] = []
    for source, names in by_source.items():
        dsn = _dsn(definitions, source)
        dialect = sources.DIALECTS[sources.kind_of(dsn)]
        runnable: list[str] = []
        # Compiling checks the SQL, in the source's dialect, before it reaches
        # the database.
        for name in names:
            try:
                compile_trigger(definitions, name, dialect=dialect)
                runnable.append(name)
            except CompileError as exc:
                errors.extend(f"trigger `{name}`: {e}" for e in exc.errors)
        try:
            with engine.connect(dsn, definitions.source.statement_timeout_ms) as conn:
                errors.extend(introspect.validate_definitions(definitions, conn, only=runnable))
        except engine.EngineError as exc:
            _fail(f"could not connect to {source or 'the default source'}", [str(exc)])

    if errors:
        _fail(f"version {version} has {len(errors)} problems", errors)

    console.print(
        f"\n[green]✓[/green] version {version} is valid — "
        f"{len(definitions.triggers)} triggers, "
        f"{len(definitions.rules)} rules\n"
    )


@cli.command(name="list")
def list_triggers() -> None:
    """List the triggers and the rules that hang off them."""
    version, definitions = _active()

    if not definitions.triggers:
        console.print(f"\n[yellow]Version {version} defines no triggers.[/yellow]\n")
        return

    triggers = Table(
        header_style="bold",
        border_style="dim",
        title=f"\ntriggers (version {version})",
        title_justify="left",
        title_style="dim",
    )
    triggers.add_column("name")
    triggers.add_column("clock")
    triggers.add_column("key")
    triggers.add_column("description", overflow="fold")
    for name, trigger in sorted(definitions.triggers.items()):
        triggers.add_row(
            name, trigger.event_time or "—", ", ".join(trigger.key), trigger.description or ""
        )
    console.print(triggers)

    rules = Table(
        header_style="bold",
        border_style="dim",
        title="\nrules",
        title_justify="left",
        title_style="dim",
    )
    rules.add_column("name")
    rules.add_column("trigger")
    rules.add_column("fires at most")
    rules.add_column("description", overflow="fold")
    for name, rule in sorted(definitions.rules.items()):
        policy = rule.policy.value
        if rule.period:
            policy += f" ({rule.period})"
        if rule.n:
            policy += f" (n={rule.n})"
        rules.add_row(name, rule.trigger, policy, rule.description or "")
    console.print(rules)
    console.print()


@cli.command()
@click.argument("rule_name")
@click.option("--days", default=90, show_default=True, help="Window length in days.")
@click.option("--sample", default=20, show_default=True, help="Rows to show.")
def run(rule_name: str, days: int, sample: int) -> None:
    """Backtest a stored rule over the last N days."""
    if days < 1:
        _fail("--days must be at least 1", [f"got {days}"])

    _, definitions = _active()
    rule = definitions.rules.get(rule_name)
    trigger = definitions.triggers.get(rule.trigger) if rule is not None else None
    dsn = _dsn(definitions, trigger.source if trigger is not None else None)

    try:
        result = engine.run(definitions, rule_name, days=days, sample=sample, dsn=dsn)
    except CompileError as exc:
        _fail(f"rule `{rule_name}` did not compile", exc.errors)
    except engine.EngineError as exc:
        _fail("backtest failed", [str(exc)])

    report.render(result, console)
    console.print()


@cli.command()
@click.argument("trigger_name")
@click.option("--days", default=90, show_default=True)
def explain(trigger_name: str, days: int) -> None:
    """Print a stored trigger's compiled SQL without executing it."""
    from datetime import datetime, timedelta

    _, definitions = _active()
    until = datetime.now(UTC)
    since = until - timedelta(days=days)

    # Rendered in the dialect of the source the trigger reads. Nothing is
    # executed, so only the DSN's scheme is looked at -- and when no source
    # can be resolved at all, Postgres, which is what explain always showed.
    trigger = definitions.triggers.get(trigger_name)
    dialect = _dialect_for(definitions, trigger.source if trigger is not None else None)

    try:
        compiled = compile_trigger(
            definitions, trigger_name, since=since, until=until, dialect=dialect
        )
    except CompileError as exc:
        _fail(f"trigger `{trigger_name}` did not compile", exc.errors)

    console.print()
    console.print(Syntax(compiled.display_sql(), "sql", theme="ansi_dark", word_wrap=True))
    client = "the mysql client" if dialect == "mysql" else "psql"
    console.print(f"\n[dim]Literals are inlined so this can be pasted into {client} as-is.[/dim]\n")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option(
    "--no-write",
    is_flag=True,
    help="Refuse to change definitions from the browser.",
)
@click.option(
    "--dev-origin",
    default=None,
    help="Allow CORS from a separate Vite dev server, e.g. http://localhost:5173",
)
@click.option(
    "--allow-non-loopback",
    is_flag=True,
    help=(
        "Bind a non-loopback address. Only correct inside a container whose "
        "port is published to the host's loopback."
    ),
)
def serve(
    host: str,
    port: int,
    no_write: bool,
    dev_origin: str | None,
    allow_non_loopback: bool,
) -> None:
    """Serve the local UI.

    A single-user tool on loopback, not a service. Binding it anywhere else
    exposes a form that runs SQL against your database to the whole network.
    """
    import uvicorn

    from .api import Session, create_app
    from .platform.db import PlatformError, check_ready

    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_non_loopback:
        err_console.print(
            f"\n[bold yellow]Refusing to bind {host}[/bold yellow]\n"
            "  This server accepts a database DSN and runs queries with it. It is\n"
            "  meant for loopback only. Inside a container, pass\n"
            "  --allow-non-loopback and publish the port to 127.0.0.1 on the host.\n"
            "  For genuine remote access, put it behind something that authenticates.\n"
        )
        sys.exit(1)

    # Fail here rather than serving a UI that cannot read or write anything.
    # A page that loads and then reports every panel as broken is a worse
    # error message than refusing to start.
    try:
        check_ready()
    except PlatformError as exc:
        _fail("cannot start without a control plane", [str(exc)])

    session = Session(allow_write=not no_write)
    app = create_app(session=session, dev_origin=dev_origin)

    console.print(f"\n[green]▸[/green] rowfire on [bold]http://{host}:{port}[/bold]")
    console.print("  definitions and connections live in the control plane\n", style="dim")

    uvicorn.run(app, host=host, port=port, log_level="warning")


def _register_platform_commands() -> None:
    """Attach the control-plane commands."""
    from .platform_cli import platform, worker

    cli.add_command(platform)
    cli.add_command(worker)

    from .cloud import cloud

    cli.add_command(cloud)


_register_platform_commands()

# `rowfire rowfire <command>` runs `rowfire <command>`. A platform that
# prepends the image's ENTRYPOINT (`rowfire`) to a command already starting
# with `rowfire` -- as Render may for preDeployCommand -- still works. Hidden,
# and sharing the same commands, so it cannot drift from the real ones.
cli.add_command(click.Group(name="rowfire", commands=cli.commands, hidden=True))


if __name__ == "__main__":
    cli()
