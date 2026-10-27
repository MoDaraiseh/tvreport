"""Market overview: index levels and weekly changes, sector performance,
breadth, and the week's top gainers / losers within the universe.

"Weekly" always means the calendar week from weekly.py: the current week on a
Friday run, the week just ended on a Monday run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from .config import Config
from .quality import bar_dates, index_is_fresh
from .schedule import SessionContext
from .weekly import WeeklyChanges, weekly_changes


@dataclass(frozen=True)
class IndexLine:
    name: str
    symbol: str  # symbol actually used
    level: float | None
    weekly_pct: float | None
    note: str | None


@dataclass(frozen=True)
class SectorLine:
    sector: str
    median_weekly_pct: float
    stocks: int


@dataclass(frozen=True)
class Breadth:
    pct_above_sma50: float
    pct_above_sma200: float
    advancers: int
    decliners: int
    unchanged: int
    measured: int  # stocks with a weekly change available


@dataclass(frozen=True)
class Overview:
    indices: list[IndexLine]
    index_weekly_source: str
    sectors: list[SectorLine]
    breadth: Breadth
    gainers: pd.DataFrame
    losers: pd.DataFrame
    weekly_source: str
    week_ending: str


def _reference_level(row: pd.Series, ctx: SessionContext) -> float:
    bar_date = bar_dates(pd.Series([row["bar_time"]])).iloc[0]
    if ctx.run_type == "monday" and bar_date == ctx.run_date and pd.notna(row["prev_close"]):
        return float(row["prev_close"])
    return float(row["close"])


def index_lines(
    indices: pd.DataFrame,
    cfg: Config,
    ctx: SessionContext,
    stored_index_weekly: Mapping[str, float] | None,
) -> tuple[list[IndexLine], WeeklyChanges]:
    frame = indices.reset_index(drop=True)
    weekly = weekly_changes(frame, ctx, stored_index_weekly)
    weekly_by_symbol = dict(zip(frame["symbol"], weekly.values, strict=True))
    rows: dict[str, pd.Series] = {str(r["symbol"]): r for _, r in frame.iterrows()}

    lines: list[IndexLine] = []
    for index in cfg.indices:
        chosen, note = None, None
        if index.symbol in rows and index_is_fresh(rows[index.symbol], ctx):
            chosen = index.symbol
        elif (
            index.fallback_symbol
            and index.fallback_symbol in rows
            and index_is_fresh(rows[index.fallback_symbol], ctx)
        ):
            chosen = index.fallback_symbol
            note = (
                f"{index.symbol} had not published this session's close yet; "
                f"showing TradingView's real-time calculation {index.fallback_symbol}"
            )
        if chosen is None:
            lines.append(
                IndexLine(
                    index.name,
                    index.symbol,
                    None,
                    None,
                    "data not updated for the expected session",
                )
            )
            continue
        row = rows[chosen]
        pct = weekly_by_symbol.get(chosen)
        lines.append(
            IndexLine(
                name=index.name,
                symbol=chosen,
                level=_reference_level(row, ctx),
                weekly_pct=None if pct is None or pd.isna(pct) else float(pct),
                note=note,
            )
        )
    return lines, weekly


def _movers(frame: pd.DataFrame, n: int, *, gainers: bool) -> pd.DataFrame:
    usable = frame.dropna(subset=["weekly_pct"])
    ordered = usable.sort_values(
        ["weekly_pct", "market_cap", "ticker"],
        ascending=[not gainers, False, True],
        kind="mergesort",
    )
    return ordered.head(n)[
        ["ticker", "description", "sector", "reference_close", "weekly_pct"]
    ].reset_index(drop=True)


def build_overview(
    universe: pd.DataFrame,
    universe_weekly: WeeklyChanges,
    indices: pd.DataFrame,
    cfg: Config,
    *,
    ctx: SessionContext,
    stored_index_weekly: Mapping[str, float] | None,
) -> Overview:
    frame = universe.assign(weekly_pct=universe_weekly.values.to_numpy())
    lines, index_weekly = index_lines(indices, cfg, ctx, stored_index_weekly)

    with_weekly = frame.dropna(subset=["weekly_pct"])
    sectors = [
        SectorLine(str(sector), float(group["weekly_pct"].median()), len(group))
        for sector, group in with_weekly.groupby("sector", sort=True)
    ]
    sectors.sort(key=lambda s: (-s.median_weekly_pct, s.sector))

    close = frame["reference_close"]
    breadth = Breadth(
        pct_above_sma50=100.0 * float((close > frame["sma50"]).mean()),
        pct_above_sma200=100.0 * float((close > frame["sma200"]).mean()),
        advancers=int((with_weekly["weekly_pct"] > 0).sum()),
        decliners=int((with_weekly["weekly_pct"] < 0).sum()),
        unchanged=int((with_weekly["weekly_pct"] == 0).sum()),
        measured=len(with_weekly),
    )
    n = cfg.report.top_movers
    return Overview(
        indices=lines,
        index_weekly_source=index_weekly.source,
        sectors=sectors,
        breadth=breadth,
        gainers=_movers(frame, n, gainers=True),
        losers=_movers(frame, n, gainers=False),
        weekly_source=universe_weekly.source,
        week_ending=universe_weekly.week_ending,
    )
