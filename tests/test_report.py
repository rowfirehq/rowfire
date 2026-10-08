"""Rendering. No database -- BacktestResult is constructed directly.

Not in the spec's file list, but the acceptance criterion "empty result sets
render cleanly rather than crashing on division by zero" is a rendering
concern, and testing it through the engine would need a database.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

from rich.console import Console

from rowfire.engine import BacktestResult
from rowfire.report import bucket, format_value, render, sparkline


def make_result(**overrides) -> BacktestResult:
    until = datetime(2026, 8, 7, tzinfo=UTC)
    defaults = dict(
        rule_name="tell_ops",
        trigger_name="order_completed",
        since=until - timedelta(days=30),
        until=until,
        days=30,
        emit_columns=["id", "total_amount"],
    )
    defaults.update(overrides)
    return BacktestResult(**defaults)


def render_to_string(result: BacktestResult) -> str:
    buffer = io.StringIO()
    render(result, Console(file=buffer, width=100, no_color=True))
    return buffer.getvalue()


def test_empty_result_renders_without_dividing_by_zero() -> None:
    output = render_to_string(make_result())
    assert "never have fired" in output


def test_empty_result_still_states_the_caveat() -> None:
    # The undercount warning is not conditional on there being fires.
    assert "undercounts" in render_to_string(make_result())


def test_null_event_time_rows_are_shown_prominently() -> None:
    output = render_to_string(make_result(matched_rows=100, fires=100, null_event_time_rows=40))
    assert "40 rows" in output
    assert "missing event_time" in output


def test_null_event_time_panel_is_absent_when_zero() -> None:
    output = render_to_string(make_result(matched_rows=10, fires=10))
    assert "missing event_time" not in output


def test_dedup_collapse_is_reported() -> None:
    output = render_to_string(make_result(matched_rows=100, fires=40))
    assert "collapsed by dedup" in output
    assert "60" in output


def test_a_trigger_with_no_clock_says_so() -> None:
    output = render_to_string(
        make_result(timeless=True, since=None, until=None, matched_rows=5, fires=5)
    )
    assert "declares no event_time" in output


def test_distribution_reports_zero_volume_days() -> None:
    result = make_result(
        matched_rows=10,
        fires=10,
        per_day={date(2026, 8, 1): 10},
    )
    assert "days with zero fires" in render_to_string(result)


def test_long_gaps_are_named() -> None:
    result = make_result(
        matched_rows=2,
        fires=2,
        per_day={date(2026, 7, 8): 1, date(2026, 8, 7): 1},
    )
    assert "longest gap" in render_to_string(result)


def test_sparkline_distinguishes_zero_from_small() -> None:
    # A quiet day and an empty day must not look the same: one means the
    # trigger works and volume is low, the other means it did not fire.
    line = sparkline([0, 1, 50])
    assert line[0] == " "
    assert line[1] != " "


def test_sparkline_handles_all_zeroes() -> None:
    assert sparkline([0, 0, 0]).strip() == ""


def test_sparkline_handles_empty_input() -> None:
    assert sparkline([]) == ""


def test_bucket_compresses_to_width() -> None:
    assert len(bucket(list(range(120)), 40)) == 40


def test_bucket_preserves_the_total() -> None:
    values = [3] * 121
    assert sum(bucket(values, 78)) == sum(values)


def test_bucket_leaves_short_series_alone() -> None:
    assert bucket([1, 2, 3], 80) == [1, 2, 3]


def test_format_value_renders_null_visibly() -> None:
    assert "null" in format_value(None)


def test_format_value_renders_booleans_as_sql_literals() -> None:
    assert format_value(False) == "false"
