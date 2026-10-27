"""Friday evaluation of the same week's Monday BUY and SELL picks.

return       = end_price / ref_price - 1, where ref_price is the close recorded at
               the Monday run and end_price is this run's latest session close
benchmark    = S&P 500 over the same window (its level at the Monday run -> now)
beat         = BUY: return > benchmark; SELL: return < benchmark (a SELL "works"
               when the stock lags the market)
hit rate     = share of picks that beat, among picks with both returns known
up rate      = BUY: share with return > 0; SELL: share with return < 0
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .history import PickRecord, PickResult


@dataclass(frozen=True)
class ListStats:
    list: str
    picks: int
    measured: int
    avg_return_pct: float | None
    avg_excess_pct: float | None
    hit_rate_pct: float | None  # beat the benchmark
    up_rate_pct: float | None  # moved the predicted way in absolute terms


def evaluate_picks(
    picks: Sequence[PickRecord],
    end_prices: Mapping[str, float],
    benchmark_return_pct: float | None,
) -> list[PickResult]:
    results: list[PickResult] = []
    for p in picks:
        end = end_prices.get(p.symbol)
        ret = None if end is None or p.ref_price <= 0 else 100.0 * (end / p.ref_price - 1.0)
        beat: bool | None = None
        if ret is not None and benchmark_return_pct is not None:
            beat = ret > benchmark_return_pct if p.list == "BUY" else ret < benchmark_return_pct
        results.append(
            PickResult(
                p.list, p.symbol, p.ticker, p.ref_price, end, ret, benchmark_return_pct, beat
            )
        )
    return results


def summarize(results: Sequence[PickResult], list_name: str) -> ListStats:
    rows = [r for r in results if r.list == list_name]
    with_return = [r for r in rows if r.return_pct is not None]
    with_beat = [r for r in rows if r.beat_benchmark is not None]
    with_excess = [r for r in with_return if r.benchmark_return_pct is not None]

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    up = (
        [r for r in with_return if (r.return_pct or 0) > 0]
        if list_name == "BUY"
        else [r for r in with_return if (r.return_pct or 0) < 0]
    )
    return ListStats(
        list=list_name,
        picks=len(rows),
        measured=len(with_return),
        avg_return_pct=mean([r.return_pct for r in with_return if r.return_pct is not None]),
        avg_excess_pct=mean(
            [
                r.return_pct - r.benchmark_return_pct
                for r in with_excess
                if r.return_pct is not None and r.benchmark_return_pct is not None
            ]
        ),
        hit_rate_pct=100.0 * sum(1 for r in with_beat if r.beat_benchmark) / len(with_beat)
        if with_beat
        else None,
        up_rate_pct=100.0 * len(up) / len(with_return) if with_return else None,
    )
