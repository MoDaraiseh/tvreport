"""Calendar-week % changes from TradingView's weekly bars.

`Perf.W` is not used: verified live on 2026-10-27, it is a rolling figure whose
reference did not match last Friday's close (for JPM and the S&P 500 it matched
the previous *day's* close). Instead, with `week_bar_time` = open of the current
weekly bar, `week_close` = its close and `prev_week_close` = the previous weekly
bar's close (matched independent closes to the cent):

* Friday run (week in review): the current weekly bar must be this week's
  (starts on or after this Monday); change = week_close / prev_week_close - 1.
* Monday run (week ahead, reporting the week just ended): if the current weekly
  bar is still last week's (starts before this Monday), change = week_close /
  prev_week_close - 1. If TradingView has already rolled to this week's bar, the
  previous week's change can't be read (close[2]|1W is not available), so it
  comes from the values the previous Friday run stored in history.db -- or is
  reported as unavailable. Never estimated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from .quality import bar_dates
from .schedule import SessionContext

WeeklySource = Literal["tradingview", "history", "tradingview+history", "unavailable"]


@dataclass(frozen=True)
class WeeklyChanges:
    values: pd.Series  # % change, indexed like the input frame (NaN = unavailable)
    source: WeeklySource
    week_ending: str  # ISO date of the week's last session (label)


def weekly_changes(
    frame: pd.DataFrame,
    ctx: SessionContext,
    stored: Mapping[str, float] | None = None,
) -> WeeklyChanges:
    """Per-row calendar-week % change for `frame` (stocks or indices).

    `stored` maps symbol -> weekly % change saved by the previous Friday run
    (week ending ctx.last_week_end_session); used only for Monday runs when
    TradingView has already rolled its weekly bar.
    """

    week_start_dates = bar_dates(frame["week_bar_time"])
    raw = 100.0 * (frame["week_close"] / frame["prev_week_close"] - 1.0)
    valid_ratio = (frame["week_close"] > 0) & (frame["prev_week_close"] > 0)

    if ctx.run_type == "friday":
        is_current_week = week_start_dates.apply(lambda d: pd.notna(d) and d >= ctx.week_start)
        values = raw.where(valid_ratio & is_current_week)
        return WeeklyChanges(
            values.astype(float), "tradingview", ctx.expected_data_session.isoformat()
        )

    week_ending = ctx.last_week_end_session.isoformat()
    still_last_week = week_start_dates.apply(lambda d: pd.notna(d) and d < ctx.week_start)
    from_tv = raw.where(valid_ratio & still_last_week).astype(float)
    from_history = frame["symbol"].map(lambda s: (stored or {}).get(s, np.nan)).astype(float)
    values = from_tv.fillna(from_history)
    used_tv, used_history = (
        bool(from_tv.notna().any()),
        bool((from_tv.isna() & from_history.notna()).any()),
    )
    if used_tv and used_history:
        source: WeeklySource = "tradingview+history"
    elif used_tv:
        source = "tradingview"
    elif used_history:
        source = "history"
    else:
        source = "unavailable"
    return WeeklyChanges(values, source, week_ending)
