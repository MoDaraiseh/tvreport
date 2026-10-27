"""Data-quality gates, staleness, and calendar-week changes."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from tests.conftest import epoch, retime, stock_row
from tvreport.quality import DataQualityError, index_is_fresh, validate_universe
from tvreport.schedule import NY, session_context
from tvreport.weekly import weekly_changes

FRI = datetime(2026, 11, 6, 16, 45, tzinfo=NY)
MON = datetime(2026, 11, 2, 8, 0, tzinfo=NY)


def friday_universe(raw: pd.DataFrame) -> pd.DataFrame:
    return retime(raw, date(2026, 11, 6), date(2026, 11, 2))


def test_real_universe_passes_and_drops_only_recent_listings(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    kept, report = validate_universe(
        friday_universe(universe_raw), ctx, cfg.universe, cfg.data_quality
    )
    assert report.data_session == date(2026, 11, 6)
    assert report.dropped_out_of_range == 0 and report.dropped_other_session == 0
    assert report.dropped_missing == report.input_rows - report.kept_rows
    assert report.missing_by_field.get("sma200", 0) >= report.dropped_missing - 2
    assert (kept["reference_close"] == kept["close"]).all()


def test_universe_size_guard(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    small = friday_universe(universe_raw).head(150)
    with pytest.raises(DataQualityError, match="outside the sane range"):
        validate_universe(small, ctx, cfg.universe, cfg.data_quality)
    big = pd.concat([friday_universe(universe_raw)] * 3, ignore_index=True)
    with pytest.raises(DataQualityError, match="outside the sane range"):
        validate_universe(big, ctx, cfg.universe, cfg.data_quality)


def test_abort_when_more_than_10_percent_dropped(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    frame = friday_universe(universe_raw)
    frame.loc[frame.index[: int(len(frame) * 0.12)], "rsi"] = np.nan
    with pytest.raises(DataQualityError, match="above the 10% limit"):
        validate_universe(frame, ctx, cfg.universe, cfg.data_quality)


def test_out_of_range_rows_dropped(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    frame = friday_universe(universe_raw)
    frame.loc[0, "rsi"] = 150.0
    frame.loc[1, "rec_all"] = 1.5
    frame.loc[2, "close"] = -1.0
    frame.loc[3, "market_cap"] = 0.0
    _, report = validate_universe(frame, ctx, cfg.universe, cfg.data_quality)
    assert report.dropped_out_of_range == 4


def test_stale_data_aborts(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    stale = retime(universe_raw, date(2026, 11, 5), date(2026, 11, 2))  # Thursday's bar on Friday
    with pytest.raises(DataQualityError, match="stale data"):
        validate_universe(stale, ctx, cfg.universe, cfg.data_quality)


def test_rows_from_another_session_are_dropped(universe_raw, cfg, calendar):
    ctx = session_context("friday", FRI, calendar)
    frame = friday_universe(universe_raw)
    frame.loc[:4, "bar_time"] = epoch(date(2026, 11, 5))
    _, report = validate_universe(frame, ctx, cfg.universe, cfg.data_quality)
    assert report.dropped_other_session == 5


def test_monday_previous_session_is_normal(universe_raw, cfg, calendar):
    ctx = session_context("monday", MON, calendar)
    frame = retime(universe_raw, date(2026, 10, 30), date(2026, 10, 26))
    kept, report = validate_universe(frame, ctx, cfg.universe, cfg.data_quality)
    assert not report.premarket_bar
    assert (kept["reference_close"] == kept["close"]).all()


def test_monday_premarket_bar_uses_previous_close(universe_raw, cfg, calendar):
    ctx = session_context("monday", MON, calendar)
    frame = retime(universe_raw, date(2026, 11, 2), date(2026, 11, 2))
    kept, report = validate_universe(frame, ctx, cfg.universe, cfg.data_quality)
    assert report.premarket_bar
    assert (kept["reference_close"] == kept["prev_close"]).all()


def test_index_freshness(calendar):
    ctx = session_context("friday", FRI, calendar)
    today = epoch(date(2026, 11, 6))
    assert index_is_fresh(pd.Series({"bar_time": today, "close": 101.0, "prev_close": 100.0}), ctx)
    # official index still showing the previous close after the close -> not fresh
    assert not index_is_fresh(
        pd.Series({"bar_time": today, "close": 100.0, "prev_close": 100.0}), ctx
    )
    old = epoch(date(2026, 11, 5))
    assert not index_is_fresh(
        pd.Series({"bar_time": old, "close": 101.0, "prev_close": 100.0}), ctx
    )


# ---------------------------------------------------------------- weekly
def _weekly_frame(week_start: date) -> pd.DataFrame:
    return pd.DataFrame(
        [
            stock_row(
                symbol="X:A",
                week_bar_time=epoch(week_start),
                week_close=110.0,
                prev_week_close=100.0,
            ),
            stock_row(
                symbol="X:B",
                week_bar_time=epoch(week_start),
                week_close=95.0,
                prev_week_close=100.0,
            ),
        ]
    )


def test_friday_weekly_change_is_week_over_week(calendar):
    ctx = session_context("friday", FRI, calendar)
    result = weekly_changes(_weekly_frame(date(2026, 11, 2)), ctx)
    assert result.values.tolist() == pytest.approx([10.0, -5.0])
    assert result.source == "tradingview" and result.week_ending == "2026-11-06"


def test_friday_rejects_a_previous_weeks_bar(calendar):
    ctx = session_context("friday", FRI, calendar)
    result = weekly_changes(_weekly_frame(date(2026, 10, 26)), ctx)
    assert result.values.isna().all()


def test_monday_uses_last_weeks_bar_when_not_rolled(calendar):
    ctx = session_context("monday", MON, calendar)
    result = weekly_changes(_weekly_frame(date(2026, 10, 26)), ctx)
    assert result.values.tolist() == pytest.approx([10.0, -5.0])
    assert result.source == "tradingview" and result.week_ending == "2026-10-30"


def test_monday_rolled_falls_back_to_history(calendar):
    ctx = session_context("monday", MON, calendar)
    result = weekly_changes(_weekly_frame(date(2026, 11, 2)), ctx, {"X:A": 3.0, "X:B": -1.0})
    assert result.values.tolist() == pytest.approx([3.0, -1.0]) and result.source == "history"


def test_monday_rolled_without_history_is_unavailable(calendar):
    ctx = session_context("monday", MON, calendar)
    result = weekly_changes(_weekly_frame(date(2026, 11, 2)), ctx, {})
    assert result.values.isna().all() and result.source == "unavailable"


def test_monday_mixed_sources(calendar):
    ctx = session_context("monday", MON, calendar)
    frame = _weekly_frame(date(2026, 10, 26))
    frame.loc[1, "week_bar_time"] = epoch(date(2026, 11, 2))  # B already rolled
    result = weekly_changes(frame, ctx, {"X:B": -2.0})
    assert result.values.tolist() == pytest.approx([10.0, -2.0])
    assert result.source == "tradingview+history"
