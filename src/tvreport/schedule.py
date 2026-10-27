"""NYSE calendar and run gating, all in America/New_York (zoneinfo, DST-aware).

GitHub Actions cron is UTC-only and can fire late, so the workflow triggers
every 15 minutes across both the EDT and EST equivalents of each window and
this module decides whether *this* trigger should do anything:

* Monday ("Week Ahead"): only on a Monday, between `earliest` and `latest` ET
  (default 06:30-09:25, i.e. before the 09:30 open). Runs on market holidays too.
* Friday ("Week in Review"): only on a Friday, no earlier than the session close
  + `minutes_after_close` (16:30 normally, 13:30 on an early-close day, 16:30 on
  a holiday) and no later than `latest` (default 18:15). This lower bound is what
  stops the EST equivalent of an EDT cron from firing at 15:45 ET, before the close.

Idempotency (one report per date + run type) is enforced separately in history.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

from .config import ScheduleConfig

NY = ZoneInfo("America/New_York")
RunType = Literal["monday", "friday"]
REGULAR_CLOSE = time(16, 0)
REGULAR_OPEN = time(9, 30)


class MarketCalendar:
    """Thin wrapper over pandas_market_calendars' NYSE calendar."""

    def __init__(self) -> None:
        self._cal = mcal.get_calendar("NYSE")

    @lru_cache(maxsize=64)  # noqa: B019 - one calendar per process; bounded cache
    def _schedule(self, year: int) -> pd.DataFrame:
        sched: pd.DataFrame = self._cal.schedule(
            start_date=f"{year - 1}-12-01", end_date=f"{year + 1}-01-31"
        )
        return sched

    def _row(self, day: date) -> pd.Series | None:
        sched = self._schedule(day.year)
        key = pd.Timestamp(day)
        if key not in sched.index:
            return None
        row = sched.loc[key]
        if not isinstance(row, pd.Series):  # duplicate index entries would yield a frame
            raise RuntimeError(f"ambiguous NYSE schedule entry for {day}")
        return row

    def is_session(self, day: date) -> bool:
        return self._row(day) is not None

    def close_time(self, day: date) -> datetime | None:
        """Session close in New York time, or None if the market is closed."""

        row = self._row(day)
        if row is None:
            return None
        close: datetime = pd.Timestamp(row["market_close"]).tz_convert(NY).to_pydatetime()
        return close

    def is_early_close(self, day: date) -> bool:
        close = self.close_time(day)
        return close is not None and close.time() < REGULAR_CLOSE

    def previous_session(self, day: date) -> date:
        """Most recent session strictly before `day`."""

        probe = day - timedelta(days=1)
        for _ in range(15):
            if self.is_session(probe):
                return probe
            probe -= timedelta(days=1)
        raise RuntimeError(f"no NYSE session in the 15 days before {day}")

    def last_completed_session(self, now: datetime) -> date:
        """Latest session whose close is at or before `now` (New York time)."""

        today = now.astimezone(NY).date()
        close = self.close_time(today)
        if close is not None and close <= now:
            return today
        return self.previous_session(today)


@dataclass(frozen=True)
class GateDecision:
    proceed: bool
    reason: str


def resolve_run_type(value: str, now: datetime) -> RunType | None:
    """'monday' / 'friday' as given; 'auto' picks by New York weekday (else None)."""

    if value in ("monday", "friday"):
        return value  # type: ignore[return-value]
    if value != "auto":
        raise ValueError(f"run type must be monday, friday or auto, not {value!r}")
    weekday = now.astimezone(NY).weekday()
    return "monday" if weekday == 0 else "friday" if weekday == 4 else None


def friday_earliest(day: date, cfg: ScheduleConfig, cal: MarketCalendar) -> datetime:
    close = cal.close_time(day) or datetime.combine(day, REGULAR_CLOSE, NY)
    return close + timedelta(minutes=cfg.friday.minutes_after_close)


def gate(
    run_type: RunType, now: datetime, cfg: ScheduleConfig, cal: MarketCalendar
) -> GateDecision:
    """Should a scheduled trigger at `now` produce the `run_type` report?"""

    local = now.astimezone(NY)
    stamp = local.strftime("%a %Y-%m-%d %H:%M %Z")
    if run_type == "monday":
        win = cfg.monday
        if local.weekday() != 0:
            return GateDecision(False, f"{stamp}: not a Monday in New York")
        if not win.earliest <= local.time() <= win.latest:
            return GateDecision(
                False,
                f"{stamp}: outside the Monday window {win.earliest:%H:%M}-{win.latest:%H:%M} ET",
            )
        return GateDecision(True, f"{stamp}: inside the Monday window")

    if local.weekday() != 4:
        return GateDecision(False, f"{stamp}: not a Friday in New York")
    earliest = friday_earliest(local.date(), cfg, cal)
    latest = datetime.combine(local.date(), cfg.friday.latest, NY)
    if not earliest <= local <= latest:
        return GateDecision(
            False, f"{stamp}: outside the Friday window {earliest:%H:%M}-{latest:%H:%M} ET"
        )
    return GateDecision(True, f"{stamp}: inside the Friday window")


@dataclass(frozen=True)
class SessionContext:
    """What the market was doing for this run, for labeling the report."""

    run_type: RunType
    run_date: date  # New York date of the run
    market_open_today: bool
    early_close_today: bool
    close_time_today: datetime | None
    expected_data_session: date  # the session the data must reflect
    week_start: date  # Monday of the run's week
    last_week_end_session: date  # last session of the previous week (Monday runs)
    notes: tuple[str, ...]


def session_context(run_type: RunType, now: datetime, cal: MarketCalendar) -> SessionContext:
    local = now.astimezone(NY)
    today = local.date()
    week_start = today - timedelta(days=today.weekday())
    open_today = cal.is_session(today)
    close = cal.close_time(today)
    early = cal.is_early_close(today)
    notes: list[str] = []

    if run_type == "monday":
        expected = cal.previous_session(today)
        if not open_today:
            notes.append(f"US markets are closed today ({today:%a %d %b %Y}) for a market holiday.")
    else:
        expected = cal.last_completed_session(local) if open_today else cal.previous_session(today)
        if not open_today:
            notes.append(
                f"US markets were closed today ({today:%a %d %b %Y}) for a market holiday; "
                f"figures use the {expected:%a %d %b} session."
            )
        elif early and close is not None:
            notes.append(
                f"Today was an early-close session (closed {close:%H:%M} ET); "
                f"figures use that shortened session."
            )
        if expected != today and open_today:
            notes.append(
                f"Today's session has not closed yet; figures use the {expected:%a %d %b} session."
            )
    last_week_end = cal.previous_session(week_start)
    return SessionContext(
        run_type=run_type,
        run_date=today,
        market_open_today=open_today,
        early_close_today=early,
        close_time_today=close,
        expected_data_session=expected,
        week_start=week_start,
        last_week_end_session=last_week_end,
        notes=tuple(notes),
    )
