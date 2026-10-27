"""Data-quality gates: fail loudly rather than send a misleading report.

* Rows missing a required field, or with an out-of-range value (RSI outside
  0..100, Recommend.* outside -1..1, non-positive price / market cap / SMA), or
  whose daily bar is from a different session than the rest, are dropped and
  counted. More than `max_drop_fraction` of the universe dropped aborts the run.
* The universe size must lie within [min_size, max_size] (a size outside that
  range almost always means the filter itself broke).
* Staleness: every row carries `bar_time`, the open of its current daily bar.
  Its New York date is the session the data reflects. The dominant session must
  be the one the NYSE calendar says is expected for this run, else abort.
  Monday pre-market: the expected session is the previous one; if TradingView
  has already opened today's (pre-market) daily bar, that is accepted too and
  `reference_close` falls back to `prev_close`, the previous session's close.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import DataQualityConfig, UniverseConfig
from .schedule import NY, SessionContext

log = logging.getLogger(__name__)

REQUIRED_STOCK_FIELDS: tuple[str, ...] = (
    "ticker",
    "description",
    "close",
    "change_pct",
    "rel_volume_10d",
    "market_cap",
    "sector",
    "rec_all",
    "rsi",
    "macd",
    "macd_signal",
    "adx",
    "sma50",
    "sma200",
    "perf_1m",
    "perf_3m",
    "high_52w",
    "low_52w",
    "prev_close",
    "bar_time",
)


class DataQualityError(RuntimeError):
    """The data is not trustworthy enough to report on."""


@dataclass(frozen=True)
class QualityReport:
    input_rows: int
    kept_rows: int
    dropped_missing: int
    dropped_out_of_range: int
    dropped_other_session: int
    data_session: date
    premarket_bar: bool  # Monday: TradingView already opened today's daily bar
    missing_by_field: dict[str, int] = field(default_factory=dict)


def bar_dates(epochs: pd.Series) -> pd.Series:
    """Epoch seconds -> New York calendar date (NaN stays missing)."""

    stamps = pd.to_datetime(epochs, unit="s", utc=True, errors="coerce")
    return stamps.dt.tz_convert(NY).dt.date


def _out_of_range(frame: pd.DataFrame) -> pd.Series:
    bad = pd.Series(False, index=frame.index)
    bad |= ~frame["rsi"].between(0, 100)
    for column in ("rec_all", "rec_ma", "rec_other"):
        present = frame[column].notna()
        bad |= present & ~frame[column].between(-1, 1)
    for column in ("close", "market_cap", "sma50", "sma200", "high_52w", "low_52w", "prev_close"):
        bad |= ~(frame[column] > 0)
    bad |= frame["high_52w"] < frame["low_52w"]
    return bad


def accepted_sessions(ctx: SessionContext) -> set[date]:
    sessions = {ctx.expected_data_session}
    if ctx.run_type == "monday" and ctx.market_open_today:
        sessions.add(ctx.run_date)  # a pre-market daily bar for today
    return sessions


def validate_universe(
    frame: pd.DataFrame,
    ctx: SessionContext,
    universe_cfg: UniverseConfig,
    dq_cfg: DataQualityConfig,
) -> tuple[pd.DataFrame, QualityReport]:
    total = len(frame)
    if not universe_cfg.min_size <= total <= universe_cfg.max_size:
        raise DataQualityError(
            f"universe has {total} stocks, outside the sane range "
            f"{universe_cfg.min_size}-{universe_cfg.max_size}; the filter is probably broken"
        )
    log.info("universe size: %d", total)

    work = frame.copy()
    missing = work[list(REQUIRED_STOCK_FIELDS)].isna().any(axis=1)
    for column in ("ticker", "description", "sector"):
        missing |= work[column].astype("string").str.strip().fillna("").eq("")
    out_of_range = ~missing & _out_of_range(work)

    dates = bar_dates(work["bar_time"])
    candidates = work.loc[~missing & ~out_of_range]
    if candidates.empty:
        raise DataQualityError("no usable rows in the universe")
    session_counts = dates[candidates.index].value_counts()
    data_session: date = session_counts.index[0]
    allowed = accepted_sessions(ctx)
    if data_session not in allowed:
        raise DataQualityError(
            f"stale data: most rows are from the {data_session} session, but this run expects "
            f"{', '.join(str(d) for d in sorted(allowed))}"
        )
    other_session = ~missing & ~out_of_range & (dates != data_session)

    dropped = missing | out_of_range | other_session
    counts = (int(missing.sum()), int(out_of_range.sum()), int(other_session.sum()))
    log.info(
        "dropped %d rows: %d missing a required field, %d out of range, %d from another session",
        int(dropped.sum()),
        *counts,
    )
    if dropped.sum() > dq_cfg.max_drop_fraction * total:
        raise DataQualityError(
            f"dropped {int(dropped.sum())} of {total} rows ({dropped.mean():.1%}), above the "
            f"{dq_cfg.max_drop_fraction:.0%} limit: {counts[0]} missing a required field, "
            f"{counts[1]} out of range, {counts[2]} from another session"
        )

    kept = work.loc[~dropped].copy()
    premarket_bar = ctx.run_type == "monday" and data_session == ctx.run_date
    kept["reference_close"] = kept["prev_close"] if premarket_bar else kept["close"]
    kept = kept.reset_index(drop=True)
    per_field = work[list(REQUIRED_STOCK_FIELDS)].isna().sum()
    missing_by_field = {str(k): int(v) for k, v in per_field.items() if v}
    report = QualityReport(total, len(kept), *counts, data_session, premarket_bar, missing_by_field)
    return kept, report


def index_is_fresh(row: pd.Series, ctx: SessionContext) -> bool:
    """An index row reflects the expected session: its bar date is accepted, and
    on a post-close Friday run it isn't still showing the previous close (an
    official index that publishes its close late reads close == prev_close)."""

    if pd.isna(row.get("bar_time")) or not row.get("close") or row["close"] <= 0:
        return False
    bar_date = bar_dates(pd.Series([row["bar_time"]])).iloc[0]
    if bar_date not in accepted_sessions(ctx):
        return False
    reflects_today = ctx.run_type == "friday" and bar_date == ctx.run_date
    unchanged = pd.notna(row.get("prev_close")) and np.isclose(
        float(row["close"]), float(row["prev_close"]), rtol=0, atol=1e-9
    )
    return not (reflects_today and unchanged)


def require_columns(frame: pd.DataFrame, columns: Sequence[str]) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise DataQualityError(f"provider frame is missing columns: {missing}")
