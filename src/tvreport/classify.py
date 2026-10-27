"""BUY / SELL / HOLD lists from scored stocks. Deterministic, rule-based.

Ordering ties are broken by market cap (descending), then ticker (ascending),
so the same input always yields the same lists.

* "extended" = RSI > extended_rsi, or close more than extended_pct_above_sma50 %
  above SMA50. "uptrend" = close > SMA200 and SMA50 > SMA200.
* SELL: lowest scores with close < SMA200 and Recommend.All <= sell_max_recommend.
* HOLD: highest scores that are extended, in an uptrend, with score >= hold_min_score
  ("if you own it, keep it; don't buy more here").
* BUY:  highest scores with close > SMA200, Recommend.All >= buy_min_recommend,
  RSI < buy_max_rsi and NOT extended; at most max_per_sector per sector.

A stock appears in at most one list: SELL needs close < SMA200 and BUY/HOLD need
close > SMA200, and extended stocks can only be HOLD, never BUY. Lists are also
filled in the order SELL, HOLD, BUY with already-listed stocks skipped, as a
belt-and-braces guarantee. Rows with any NaN in a field a rule needs are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import ClassifyConfig

_NEEDED = ("score", "reference_close", "sma50", "sma200", "rec_all", "rsi", "sector", "market_cap")


@dataclass(frozen=True)
class Classification:
    buy: pd.DataFrame
    sell: pd.DataFrame
    hold: pd.DataFrame


def _ordered(frame: pd.DataFrame, *, best_first: bool) -> pd.DataFrame:
    return frame.sort_values(
        ["score", "market_cap", "ticker"],
        ascending=[not best_first, False, True],
        kind="mergesort",
    )


def classify(scored: pd.DataFrame, cfg: ClassifyConfig) -> Classification:
    usable = scored.dropna(subset=list(_NEEDED))
    close, sma50, sma200 = usable["reference_close"], usable["sma50"], usable["sma200"]
    extended = (usable["rsi"] > cfg.extended_rsi) | (
        100.0 * (close / sma50 - 1.0) > cfg.extended_pct_above_sma50
    )
    uptrend = (close > sma200) & (sma50 > sma200)
    taken: set[str] = set()

    def take(
        candidates: pd.DataFrame, n: int, *, best_first: bool, sector_cap: int | None
    ) -> pd.DataFrame:
        chosen: list[int] = []
        per_sector: dict[str, int] = {}
        for idx, row in _ordered(candidates, best_first=best_first).iterrows():
            if len(chosen) >= n:
                break
            symbol = str(row["symbol"])
            if symbol in taken:
                continue
            sector = str(row["sector"])
            if sector_cap is not None and per_sector.get(sector, 0) >= sector_cap:
                continue
            per_sector[sector] = per_sector.get(sector, 0) + 1
            taken.add(symbol)
            chosen.append(idx)  # type: ignore[arg-type]
        out = candidates.loc[chosen].copy()
        out.insert(0, "rank", range(1, len(out) + 1))
        return out.reset_index(drop=True)

    sell = take(
        usable[(close < sma200) & (usable["rec_all"] <= cfg.sell_max_recommend)],
        cfg.n_sell,
        best_first=False,
        sector_cap=None,
    )
    hold = take(
        usable[extended & uptrend & (usable["score"] >= cfg.hold_min_score)],
        cfg.n_hold,
        best_first=True,
        sector_cap=None,
    )
    buy = take(
        usable[
            (close > sma200)
            & (usable["rec_all"] >= cfg.buy_min_recommend)
            & (usable["rsi"] < cfg.buy_max_rsi)
            & ~extended
        ],
        cfg.n_buy,
        best_first=True,
        sector_cap=cfg.max_per_sector,
    )
    return Classification(buy=buy, sell=sell, hold=hold)
