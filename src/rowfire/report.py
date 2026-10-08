"""Statistics, the per-day series, and rich rendering.

All terminal output for `run` lives here. The guiding idea from the concept
doc is validation by recognition rather than comprehension: the reviewer should
be able to confirm the rows look right without reading the SQL. So the sample
table and the distribution are the centrepiece, and the caveats are stated
plainly rather than buried.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .engine import BacktestResult

_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    """Render counts as block characters.

    Zero maps to a space rather than the lowest block, so a genuinely empty
    day is visually distinct from a quiet one -- which is the difference
    between "nothing happened" and "something small happened".
    """
    if not values:
        return ""
    peak = max(values)
    if peak == 0:
        return " " * len(values)
    out = []
    for value in values:
        if value == 0:
            out.append(" ")
        else:
            index = 1 + int((value / peak) * (len(_BLOCKS) - 2))
            out.append(_BLOCKS[min(index, len(_BLOCKS) - 1)])
    return "".join(out)


def format_value(value: Any) -> str:
    if value is None:
        return "[dim]null[/dim]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def render(result: BacktestResult, console: Console) -> None:
    console.print()
    console.print(
        Panel(
            _headline(result),
            title=f"[bold]{result.rule_name}[/bold]",
            subtitle=f"trigger: {result.trigger_name}",
            border_style="cyan",
        )
    )

    _render_stats(result, console)

    if result.timeless:
        console.print(
            "\n[yellow]This trigger declares no event_time[/yellow] — reporting "
            "totals only. Per-day distribution is unavailable."
        )
    elif result.per_day:
        _render_distribution(result, console)
    else:
        console.print("\n[dim]No fires in this window, so no distribution.[/dim]")

    _render_sample(result, console)
    _render_caveats(result, console)


def _headline(result: BacktestResult) -> Text:
    if result.fires == 0:
        body = Text("Would never have fired in this window.", style="yellow")
    else:
        body = Text.from_markup(
            f"Would have fired [bold cyan]{result.fires:,}[/bold cyan] times "
            f"over [bold]{result.days}[/bold] days, across "
            f"[bold]{result.unique_keys:,}[/bold] distinct keys."
        )
    if result.since and result.until:
        body.append(f"\n{result.since:%Y-%m-%d} → {result.until:%Y-%m-%d}", style="dim")
    return body


def _render_stats(result: BacktestResult, console: Console) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="dim", justify="right")
    table.add_column(style="bold")

    table.add_row("rows the query returned", f"{result.matched_rows:,}")
    table.add_row("fires after dedup", f"{result.fires:,}")
    if result.dedup_removed:
        table.add_row("collapsed by dedup", f"{result.dedup_removed:,}")
    table.add_row("distinct keys", f"{result.unique_keys:,}")

    if not result.timeless:
        table.add_row("mean per day", f"{result.mean_per_day:.1f}")
        busiest = result.busiest_day()
        if busiest:
            table.add_row("busiest day", f"{busiest[0]} ({busiest[1]:,})")
        quietest = result.quietest_day()
        if quietest:
            table.add_row("quietest day", f"{quietest[0]} ({quietest[1]:,})")

    table.add_row("query time", f"{result.query_ms:.0f} ms")
    console.print()
    console.print(table)


def bucket(values: list[int], width: int) -> list[int]:
    """Compress a series to at most `width` columns by summing adjacent days.

    A 120-day window does not fit an 80-column terminal, and letting it wrap
    turns the sparkline into two meaningless rows with a misaligned axis.
    """
    if width < 1 or len(values) <= width:
        return values
    step = len(values) / width
    out: list[int] = []
    for i in range(width):
        start = int(i * step)
        end = int((i + 1) * step) if i < width - 1 else len(values)
        out.append(sum(values[start : max(end, start + 1)]))
    return out


def _render_distribution(result: BacktestResult, console: Console) -> None:
    days = result.day_range()
    values = [result.per_day.get(day, 0) for day in days]

    # Leave a couple of columns of headroom so the line never wraps.
    width = max(console.width - 2, 20)
    rendered = bucket(values, width)
    per_column = len(values) / len(rendered) if rendered else 1

    unit = "day" if per_column <= 1.5 else f"{per_column:.0f} days"
    console.print()
    console.print(f"[dim]fires per {unit}[/dim]")
    console.print(f"[cyan]{sparkline(rendered)}[/cyan]")

    if days:
        left = f"{days[0]:%b %d}"
        right = f"{days[-1]:%b %d}"
        pad = max(len(rendered) - len(left) - len(right), 1)
        console.print(f"[dim]{left}{' ' * pad}{right}[/dim]")

    empty = [d for d, v in zip(days, values, strict=False) if v == 0]
    if empty:
        console.print(
            f"[dim]{len(empty)} of {len(days)} days with zero fires{_describe_gap(empty)}[/dim]"
        )


def _describe_gap(empty_days: list[date]) -> str:
    """Name the longest unbroken silent stretch.

    A scattering of quiet days is normal; three unbroken weeks usually means
    the predicate is wrong or the data has a hole worth knowing about.
    """
    if not empty_days:
        return ""
    longest_start = run_start = empty_days[0]
    longest = run = 1
    for previous, current in zip(empty_days, empty_days[1:], strict=False):
        if (current - previous).days == 1:
            run += 1
        else:
            run_start, run = current, 1
        if run > longest:
            longest, longest_start = run, run_start
    if longest < 3:
        return ""
    return f", longest gap {longest} days from {longest_start:%b %d}"


def _render_sample(result: BacktestResult, console: Console) -> None:
    if not result.sample:
        return

    table = Table(
        title=f"\nsample of {len(result.sample)} matched rows",
        title_style="dim",
        title_justify="left",
        header_style="bold",
        border_style="dim",
    )
    for column in result.emit_columns:
        table.add_column(column, overflow="fold")

    for row in result.sample:
        table.add_row(*[format_value(row.get(c)) for c in result.emit_columns])

    console.print(table)


def _render_caveats(result: BacktestResult, console: Console) -> None:
    console.print()

    if result.null_event_time_rows:
        share = (
            result.null_event_time_rows / max(result.matched_rows + result.null_event_time_rows, 1)
        ) * 100
        severity = "red" if share > 20 else "yellow"
        console.print(
            Panel(
                Text.from_markup(
                    f"[bold]{result.null_event_time_rows:,} rows[/bold] match this "
                    f"trigger but have no event_time ({share:.0f}% of all matches).\n"
                    f"They cannot be placed on the timeline and are [bold]not[/bold] "
                    f"counted above.\n\n"
                    f"[dim]A large share here usually means the wrong output "
                    f"column was named as this trigger's event_time.[/dim]"
                ),
                title="[bold]rows missing event_time[/bold]",
                border_style=severity,
            )
        )

    console.print(
        Panel(
            Text.from_markup(
                "This backtest evaluates rows as they are [bold]now[/bold], and "
                "uses event_time to place them in the past.\n"
                "It does not replay history, so it [bold]undercounts[/bold]: a "
                "row that matched at the time but has since moved on\n"
                "(an order completed in March and refunded in April) is invisible "
                "here. Treat the count as a floor."
            ),
            title="[bold]what this number is not[/bold]",
            border_style="yellow",
        )
    )
