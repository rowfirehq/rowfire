"""CLI for the control plane: `rowfire platform …` and `rowfire worker`.

Kept out of cli.py so the read-only v0 surface stays separable from the part
that stores credentials and sends messages.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from sqlmodel import select

from .platform import actions, crypto, scheduler, store
from .platform.db import PlatformError, get_engine, session_scope
from .platform.models import DefinitionVersion, Mode, RuleState, TriggerState

console = Console()
err_console = Console(stderr=True)


def _fail(title: str, lines: list[str]) -> None:
    err_console.print(f"\n[bold red]{title}[/bold red]")
    for line in lines:
        err_console.print(f"  [red]•[/red] {line}")
    err_console.print()
    sys.exit(1)


@click.group()
def platform() -> None:
    """Manage the control plane: connections, definitions, trigger state."""


@platform.command(name="init-key")
def init_key() -> None:
    """Print a fresh master key for ROWFIRE_MASTER_KEY."""
    console.print(crypto.generate_master_key())
    console.print(
        "\n[yellow]Store this somewhere durable.[/yellow] Losing it makes every "
        "stored credential unrecoverable.\n",
        style="dim",
    )


@platform.command()
@click.option(
    "--dsn-env",
    default="DATABASE_URL",
    show_default=True,
    help="Env var holding the customer DSN. Never pass the DSN as an argument.",
)
@click.option(
    "--name",
    default="primary",
    show_default=True,
    help="Data source name. A trigger reads it with `source: <name>`; `primary` is the default.",
)
def connect(dsn_env: str, name: str) -> None:
    """Store a data source (Postgres, MySQL or Supabase), encrypted."""
    import re

    from .sources import EngineError, kind_of

    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        _fail(f"`{name}` is not a valid source name", ["Use lowercase letters, digits and _."])
    dsn = os.environ.get(dsn_env)
    if not dsn:
        _fail(f"{dsn_env} is not set", ["Export the read-only DSN there first."])
    try:
        kind = kind_of(dsn)
    except EngineError as exc:
        _fail("unsupported database", [str(exc)])

    try:
        with session_scope() as session:
            connection = store.save_connection(session, dsn, name=name)
            summary = connection.name
    except (PlatformError, crypto.CryptoError) as exc:
        _fail("could not store the connection", [str(exc)])

    console.print(f"\n[green]✓[/green] stored {kind} source [bold]{summary}[/bold]")
    console.print("  the DSN is encrypted at rest; the plaintext is never written\n", style="dim")


@platform.command(name="sources")
def list_sources() -> None:
    """List the stored data sources. Never prints a credential."""
    from .sources import LABELS, EngineError, kind_of, summarise

    try:
        with session_scope() as session:
            default = store.default_source(session)
            rows = []
            for connection in store.list_connections(session):
                try:
                    dsn = store.reveal_dsn(connection)
                    kind, where = LABELS[kind_of(dsn)], summarise(dsn)
                except (crypto.CryptoError, EngineError):
                    kind, where = "?", "cannot be read with this master key"
                rows.append((connection.name, kind, where, connection.name == default))
    except PlatformError as exc:
        _fail("cannot reach the control plane", [str(exc)])

    if not rows:
        console.print(
            "\n[dim]No data sources yet. Add one with `rowfire platform connect`.[/dim]\n"
        )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("source")
    table.add_column("database")
    table.add_column("where")
    table.add_column("")
    for name, kind, where, is_default in rows:
        table.add_row(name, kind, where, "default" if is_default else "")
    console.print(table)


@platform.command()
@click.option(
    "--file",
    "-f",
    "definitions_path",
    default="definitions.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--note", default=None, help="Why this version exists.")
@click.option(
    "--if-empty",
    is_flag=True,
    help="Do nothing when definitions already exist. For idempotent seeding.",
)
def push(definitions_path: Path, note: str | None, if_empty: bool) -> None:
    """Import definitions from a file as a new version.

    The file is an import artifact, not the store of record -- nothing reads it
    again after this. `pull` is the way back out.
    """
    text = definitions_path.read_text()
    try:
        with session_scope() as session:
            if if_empty:
                existing = store.active_definitions(session)
                if existing is not None:
                    # Seeding runs on every `docker compose up`, so saying
                    # nothing happened has to be a success, not an error.
                    console.print(
                        f"\n[dim]version {existing[0].version} already stored; "
                        f"--if-empty, so nothing was imported[/dim]\n"
                    )
                    return
            version = store.save_definitions(session, text, note=note)
            number = version.version
            # Read into plain tuples *inside* the session. ORM instances
            # expire on commit, so touching them after the scope closes
            # raises DetachedInstanceError.
            clocks = {t.trigger_name: t for t in session.exec(select(TriggerState)).all()}
            rows = [
                (
                    state.rule_name,
                    state.mode.value,
                    clocks[state.trigger_name].watermark.strftime("%Y-%m-%d %H:%M")
                    if clocks.get(state.trigger_name) and clocks[state.trigger_name].watermark
                    else "—",
                )
                for state in session.exec(select(RuleState)).all()
            ]
    except PlatformError as exc:
        _fail("could not store definitions", [str(exc)])

    console.print(f"\n[green]✓[/green] stored version [bold]{number}[/bold]")
    table = Table(header_style="bold", border_style="dim")
    table.add_column("rule")
    table.add_column("mode")
    table.add_column("watermark")
    for name, mode, watermark in sorted(rows):
        colour = "yellow" if mode == Mode.shadow.value else "green"
        table.add_row(name, f"[{colour}]{mode}[/{colour}]", watermark)
    console.print(table)
    console.print(
        "\n[dim]New rules start in shadow, and their trigger's watermark starts at "
        "now -- so connecting to years of history fires nothing.[/dim]\n"
    )


@platform.command()
@click.option(
    "--out",
    "-o",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write here instead of to stdout.",
)
@click.option("--version", "wanted", default=None, type=int, help="An older version.")
def pull(out_path: Path | None, wanted: int | None) -> None:
    """Export the stored definitions as YAML.

    The counterpart to `push`. Every version is kept, so this is also how you
    read back what was in force before a change -- and how definitions get into
    version control, which is the one job a file is still good at.
    """
    with session_scope() as session:
        if wanted is None:
            found = store.active_definitions(session)
            if found is None:
                _fail("nothing stored", ["Run `rowfire init` or `platform push` first."])
            row, _ = found
            text, number = row.yaml_text, row.version
        else:
            row = session.exec(
                select(DefinitionVersion).where(DefinitionVersion.version == wanted)
            ).first()
            if row is None:
                _fail(f"no version {wanted}", ["`platform status` lists what exists."])
            text, number = row.yaml_text, row.version

    if out_path is None:
        # Straight to stdout with no decoration, so `pull > file` works.
        click.echo(text, nl=False)
        return

    out_path.write_text(text)
    console.print(f"\n[green]✓[/green] wrote version [bold]{number}[/bold] to {out_path}\n")


@platform.command()
def status() -> None:
    """Show trigger state and recent activity."""
    with session_scope() as session:
        workspace = store.ensure_workspace(session)
        rules = session.exec(select(RuleState)).all()
        clocks = {t.trigger_name: t for t in session.exec(select(TriggerState)).all()}

        if workspace.halted:
            console.print(
                f"\n[bold red]HALTED[/bold red] — {workspace.halted_reason or 'no reason given'}"
            )

        table = Table(header_style="bold", border_style="dim")
        for column in ("rule", "trigger", "mode", "watermark", "next run", "last error"):
            table.add_column(column, overflow="fold")

        for state in sorted(rules, key=lambda s: s.rule_name):
            colour = "yellow" if state.mode is Mode.shadow else "green"
            clock = clocks.get(state.trigger_name)
            table.add_row(
                state.rule_name,
                state.trigger_name,
                f"[{colour}]{state.mode.value}[/{colour}]",
                clock.watermark.strftime("%m-%d %H:%M") if clock and clock.watermark else "—",
                clock.next_run_at.strftime("%m-%d %H:%M") if clock and clock.next_run_at else "—",
                (clock.last_error or "")[:50] if clock else "",
            )
        console.print()
        console.print(table)
        console.print()


@platform.command()
@click.argument("rule_name")
def promote(rule_name: str) -> None:
    """Promote a rule from shadow to live."""
    with session_scope() as session:
        state = store.set_mode(session, rule_name, Mode.live)
        name = state.rule_name
    console.print(f"\n[green]▲[/green] [bold]{name}[/bold] is now [green]live[/green]")
    console.print("  any edit to its definition drops it back to shadow\n", style="dim")


@platform.command()
@click.argument("rule_name")
def demote(rule_name: str) -> None:
    """Return a rule to shadow mode."""
    with session_scope() as session:
        store.set_mode(session, rule_name, Mode.shadow)
    console.print(f"\n[yellow]▼[/yellow] [bold]{rule_name}[/bold] is back in shadow\n")


@platform.command()
@click.argument("reason")
def halt(reason: str) -> None:
    """Kill switch: stop everything, immediately."""
    with session_scope() as session:
        store.halt(session, reason)
    console.print(f"\n[bold red]HALTED[/bold red] — {reason}\n")


@platform.command()
def resume() -> None:
    """Lift the kill switch."""
    with session_scope() as session:
        store.resume(session)
    console.print("\n[green]✓[/green] resumed\n")


@click.command()
@click.option("--interval", default=5.0, show_default=True, help="Seconds between ticks.")
@click.option("--once", is_flag=True, help="Run a single tick and exit.")
@click.option("--max-ticks", default=0, help="Stop after this many ticks (0 = run forever).")
def worker(interval: float, once: bool, max_ticks: int) -> None:
    """Run the polling worker.

    Leases one due trigger at a time with SKIP LOCKED, so several workers can
    run side by side without coordinating.
    """
    try:
        master_key = crypto.load_master_key()
        engine = get_engine()
    except (crypto.CryptoError, PlatformError) as exc:
        _fail("worker cannot start", [str(exc)])

    console.print(f"\n[green]▸[/green] worker [bold]{scheduler.WORKER_ID}[/bold]")
    console.print(f"  polling every {interval}s\n", style="dim")

    from . import hosted

    reap_every = 600.0
    last_reap = 0.0

    ticks = 0
    while True:
        # A hosted demo's idle visitor workspaces, deleted every ten minutes.
        if hosted.enabled() and time.monotonic() - last_reap > reap_every:
            last_reap = time.monotonic()
            try:
                with session_scope(engine) as session:
                    reaped = hosted.reap(session)
                if reaped:
                    console.print(f"[dim]reaped {reaped} idle visitor workspace(s)[/dim]")
            except Exception as exc:  # noqa: BLE001 -- the worker must keep polling
                console.print(f"[red]reap failed[/red] — {str(exc)[:200]}")

        with session_scope(engine) as session:
            # Every workspace: one on a local install, one per visitor on a
            # hosted demo. The trigger leased decides which.
            outcome = scheduler.tick(
                session, master_key=master_key, deliver=actions.deliver, workspace_id=None
            )

        if outcome is not None:
            _report(outcome)

        ticks += 1
        if once or (max_ticks and ticks >= max_ticks):
            break
        if outcome is None:
            time.sleep(interval)


def _report(outcome: scheduler.PollOutcome) -> None:
    """One line for the poll, then one per rule that saw its rows."""
    stamp = time.strftime("%H:%M:%S")
    if outcome.skipped:
        console.print(
            f"[dim]{stamp}[/dim] {outcome.trigger_name} "
            f"[yellow]skipped[/yellow] — {outcome.skipped}"
        )
        return
    if outcome.error:
        console.print(
            f"[dim]{stamp}[/dim] {outcome.trigger_name} [red]failed[/red] — {outcome.error}"
        )
        return

    console.print(
        f"[dim]{stamp}[/dim] [bold]{outcome.trigger_name}[/bold] "
        f"matched={outcome.matched_rows}"
        + (f" no_clock={outcome.null_event_time_rows}" if outcome.null_event_time_rows else "")
    )
    for rule in outcome.rules:
        colour = "yellow" if rule.mode is Mode.shadow else "green"
        console.print(
            f"           ↳ {rule.rule_name} [{colour}]{rule.mode.value}[/{colour}] "
            f"[bold]fired={rule.fires_new}[/bold] already={rule.fires_suppressed}"
        )
