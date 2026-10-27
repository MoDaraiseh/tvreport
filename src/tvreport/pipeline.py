"""Build one report end to end (no sending, no history writes -- see cli.py)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .classify import classify
from .config import Config
from .history import History, PickRecord, PickResult, RunRecord, StoredRun
from .overview import build_overview
from .performance import ListStats, evaluate_picks, summarize
from .portfolio import evaluate_portfolio, load_holdings
from .providers import DataProvider
from .quality import QualityReport, validate_universe
from .report import PerformanceSection, ReportData
from .schedule import MarketCalendar, RunType, SessionContext, session_context
from .scoring import SUBSCORES, rationale, score_frame
from .weekly import WeeklyChanges, weekly_changes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOutcome:
    report: ReportData
    run_record: RunRecord
    picks: list[PickRecord]
    weekly_to_store: dict[str, tuple[str, float]]
    pick_results: list[PickResult]
    monday_run: StoredRun | None


def _weekly_label(ctx: SessionContext, weekly: WeeklyChanges) -> str:
    if ctx.run_type == "friday":
        start = ctx.week_start
        end = date.fromisoformat(weekly.week_ending)
        return f"this week, Mon {start.day} {start:%b} to the {end:%a} {end.day} {end:%b} close"
    end = ctx.last_week_end_session
    base = f"last week, ending {end:%a} {end.day} {end:%b}"
    return {
        "tradingview": base,
        "history": f"{base} (from the values stored by that week's Friday report)",
        "tradingview+history": (
            f"{base} (partly from the values stored by that week's Friday report)"
        ),
        "unavailable": f"{base} — unavailable: TradingView had already started this week's bar and "
        "no Friday report was stored for that week",
    }[weekly.source]


def _pick_rows(
    frame: pd.DataFrame, weekly_by_symbol: dict[str, float], *, include_volume: bool
) -> list[dict[str, Any]]:
    rows = []
    for _, r in frame.iterrows():
        wk = weekly_by_symbol.get(str(r["symbol"]))
        rows.append(
            {
                "rank": int(r["rank"]),
                "symbol": str(r["symbol"]),
                "ticker": str(r["ticker"]),
                "description": str(r["description"]),
                "sector": str(r["sector"]),
                "price": float(r["reference_close"]),
                "score": float(r["score"]),
                "weekly_pct": None if wk is None or pd.isna(wk) else float(wk),
                "perf_1m": None if pd.isna(r["perf_1m"]) else float(r["perf_1m"]),
                "rationale": rationale(r, include_volume=include_volume),
                "subscores": {k: round(float(r[k]), 4) for k in SUBSCORES},
            }
        )
    return rows


def _dropped_note(quality: QualityReport) -> str:
    dropped = quality.input_rows - quality.kept_rows
    parts = []
    no_sma200 = quality.missing_by_field.get("sma200", 0)
    if no_sma200:
        parts.append(f"{no_sma200} without a 200-day SMA (listed fewer than ~200 sessions ago)")
    other_missing = quality.dropped_missing - no_sma200
    if other_missing > 0:
        parts.append(f"{other_missing} missing another required field")
    if quality.dropped_out_of_range:
        parts.append(f"{quality.dropped_out_of_range} with out-of-range values")
    if quality.dropped_other_session:
        parts.append(f"{quality.dropped_other_session} with data from a different session")
    return f"{dropped} stocks were excluded from scoring: {'; '.join(parts)}."


def _pick_records(list_name: str, rows: list[dict[str, Any]]) -> list[PickRecord]:
    return [
        PickRecord(
            list_name,
            r["rank"],
            r["symbol"],
            r["ticker"],
            r["description"],
            r["sector"],
            r["price"],
            r["score"],
            r["subscores"],
            r["rationale"],
        )
        for r in rows
    ]


def build_report(
    run_type: RunType,
    now: datetime,
    cfg: Config,
    provider: DataProvider,
    history: History,
    *,
    dry_run: bool,
    holdings_path: Path | None = None,
    calendar: MarketCalendar | None = None,
) -> RunOutcome:
    cal = calendar or MarketCalendar()
    ctx = session_context(run_type, now, cal)
    log.info(
        "building %s report for %s (data session expected: %s)",
        run_type,
        ctx.run_date,
        ctx.expected_data_session,
    )

    universe, quality = validate_universe(
        provider.fetch_universe(cfg.universe), ctx, cfg.universe, cfg.data_quality
    )
    index_symbols = [i.symbol for i in cfg.indices] + [
        i.fallback_symbol for i in cfg.indices if i.fallback_symbol
    ]
    indices = provider.fetch_indices(index_symbols)

    stored_stock = stored_index = None
    if run_type == "monday":
        stored_stock = history.stored_weekly(ctx.last_week_end_session, "stock")
        stored_index = history.stored_weekly(ctx.last_week_end_session, "index")
    universe_weekly = weekly_changes(universe, ctx, stored_stock)
    weekly_by_symbol = dict(zip(universe["symbol"], universe_weekly.values, strict=True))

    volume_available = not quality.premarket_bar
    scored = score_frame(universe, cfg.weights, volume_available=volume_available)
    lists = classify(scored, cfg.classify)
    overview = build_overview(
        universe, universe_weekly, indices, cfg, ctx=ctx, stored_index_weekly=stored_index
    )

    buy = _pick_rows(lists.buy, weekly_by_symbol, include_volume=volume_available)
    sell = _pick_rows(lists.sell, weekly_by_symbol, include_volume=volume_available)
    hold = _pick_rows(lists.hold, weekly_by_symbol, include_volume=volume_available)

    portfolio_rows: list[dict[str, Any]] | None = None
    holdings = load_holdings(holdings_path)
    if holdings is not None:
        portfolio = evaluate_portfolio(
            holdings,
            scored,
            universe,
            provider=provider,
            weights=cfg.weights,
            cfg=cfg.portfolio,
            premarket_bar=quality.premarket_bar,
            volume_available=volume_available,
        )
        portfolio_rows = [
            {str(k): (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
            for row in portfolio.to_dict("records")
        ]

    bench_name = next(i.name for i in cfg.indices if i.symbol == cfg.benchmark_symbol)
    benchmark_level = next(line.level for line in overview.indices if line.name == bench_name)

    performance: PerformanceSection | None = None
    results: list[PickResult] = []
    monday_run: StoredRun | None = None
    if run_type == "friday":
        monday_run = history.monday_run_between(ctx.week_start, ctx.run_date)
        note = None
        rows: list[dict[str, Any]] = []
        week_stats: list[ListStats] = []
        bench_ret: float | None = None
        if monday_run is None:
            note = "No Monday report was recorded this week, so there are no picks to evaluate."
        else:
            picks = history.picks_for(monday_run.id, ("BUY", "SELL"))
            end_prices = {
                str(s): float(p)
                for s, p in zip(universe["symbol"], universe["reference_close"], strict=True)
            }
            missing = [p.symbol for p in picks if p.symbol not in end_prices]
            if missing:
                extra = provider.fetch_stocks(missing)
                for _, r in extra.iterrows():
                    if pd.notna(r["close"]) and r["close"] > 0:
                        end_prices[str(r["symbol"])] = float(r["close"])
            if monday_run.benchmark_ref and benchmark_level:
                bench_ret = 100.0 * (benchmark_level / monday_run.benchmark_ref - 1.0)
            else:
                note = "The S&P 500 level was unavailable, so picks can't be compared with it."
            results = evaluate_picks(picks, end_prices, bench_ret)
            rows = [
                {
                    "list": r.list,
                    "ticker": r.ticker,
                    "ref_price": r.ref_price,
                    "end_price": r.end_price,
                    "return_pct": r.return_pct,
                    "beat": r.beat_benchmark,
                }
                for r in results
            ]
            week_stats = [summarize(results, "BUY"), summarize(results, "SELL")]
        past = history.all_results(excluding_friday_on=ctx.run_date)
        cumulative = [summarize([*past, *results], "BUY"), summarize([*past, *results], "SELL")]
        performance = PerformanceSection(
            monday_run_date=monday_run.run_date if monday_run else None,
            rows=rows,
            week_stats=week_stats,
            cumulative_stats=cumulative,
            benchmark_return_pct=bench_ret,
            note=note,
        )

    # With a pre-market daily bar, prices are the previous session's closes (close[1]).
    price_session = ctx.expected_data_session if quality.premarket_bar else quality.data_session
    data_notes = [
        f"Universe: {quality.input_rows} US common stocks on "
        f"{', '.join(cfg.universe.exchanges)} with "
        f"market cap ≥ ${cfg.universe.min_market_cap / 1e9:.0f}B, 10-day average volume ≥ "
        f"{cfg.universe.min_avg_volume_10d / 1e6:.0f}M shares and "
        f"price ≥ ${cfg.universe.min_price:.0f}; "
        f"{quality.kept_rows} kept after data-quality checks.",
        f"Prices are closes from the {price_session:%a %d %b %Y} session.",
        "TradingView data is delayed (typically 15 minutes).",
    ]
    if quality.premarket_bar:
        data_notes.append(
            "TradingView had already opened today's daily bar, so prices are the previous "
            "session's closes and the volume sub-score is neutral (50) for every stock."
        )
    dropped = quality.input_rows - quality.kept_rows
    if dropped:
        data_notes.append(_dropped_note(quality))

    report = ReportData(
        run_type=run_type,
        run_date=ctx.run_date,
        generated_at=now,
        display_timezones=list(cfg.report.display_timezones),
        session_notes=list(ctx.notes),
        data_notes=data_notes,
        overview=overview,
        buy=buy,
        sell=sell,
        hold=hold,
        universe_size=quality.kept_rows,
        portfolio=portfolio_rows,
        performance=performance,
        dry_run=dry_run,
        extra={"weekly_label": _weekly_label(ctx, universe_weekly)},
    )

    weekly_to_store: dict[str, tuple[str, float]] = {}
    if run_type == "friday":
        for sym, pct in weekly_by_symbol.items():
            if pd.notna(pct):
                weekly_to_store[str(sym)] = ("stock", float(pct))
        index_weekly = weekly_changes(indices, ctx)
        for sym, pct in zip(indices["symbol"], index_weekly.values, strict=True):
            if pd.notna(pct):
                weekly_to_store[str(sym)] = ("index", float(pct))

    record = RunRecord(
        run_date=ctx.run_date,
        run_type=run_type,
        generated_at_utc=now.astimezone(UTC).isoformat(timespec="seconds"),
        data_session=quality.data_session,
        benchmark_symbol=cfg.benchmark_symbol,
        benchmark_ref=benchmark_level,
        universe_size=quality.kept_rows,
    )
    picks = _pick_records("BUY", buy) + _pick_records("SELL", sell) + _pick_records("HOLD", hold)
    return RunOutcome(report, record, picks, weekly_to_store, results, monday_run)


def record_outcome(history: History, outcome: RunOutcome) -> int:
    """Store a built report's run, picks, weekly changes and pick results."""

    return history.record_run(
        outcome.run_record,
        outcome.picks,
        weekly=outcome.weekly_to_store or None,
        week_ending=outcome.run_record.data_session if outcome.weekly_to_store else None,
        results=outcome.pick_results,
        monday_run_id=outcome.monday_run.id if outcome.monday_run else None,
    )
