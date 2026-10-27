"""Classification edge cases: ties, NaNs, sector cap, multi-list candidates."""

from __future__ import annotations

from typing import Any

import pandas as pd

from tests.conftest import stock_row
from tvreport.classify import classify
from tvreport.config import ClassifyConfig, Weights
from tvreport.scoring import score_frame


def buyable(sym: str, score: float, **kw: Any) -> dict[str, Any]:
    """Passes every BUY filter: close > SMA200, rec 0.2, RSI 55, ~5% above SMA50."""

    base = dict(
        symbol=f"X:{sym}",
        ticker=sym,
        score=score,
        reference_close=100.0,
        sma50=95.0,
        sma200=90.0,
        rec_all=0.2,
        rsi=55.0,
    )
    return stock_row(**{**base, **kw})


def sellable(sym: str, score: float, **kw: Any) -> dict[str, Any]:
    base = dict(
        symbol=f"X:{sym}",
        ticker=sym,
        score=score,
        reference_close=80.0,
        sma50=90.0,
        sma200=100.0,
        rec_all=-0.4,
        rsi=35.0,
    )
    return stock_row(**{**base, **kw})


def cfg(**kw) -> ClassifyConfig:
    return ClassifyConfig(**kw)


def symbols(frame: pd.DataFrame) -> list[str]:
    return frame["ticker"].tolist()


def test_ties_broken_by_market_cap_then_ticker():
    frame = pd.DataFrame(
        [
            buyable("CCC", 80, market_cap=5e9, sector="A"),
            buyable("BBB", 80, market_cap=9e9, sector="B"),
            buyable("AAA", 80, market_cap=5e9, sector="C"),
            buyable("DDD", 90, market_cap=1e9, sector="D"),
        ]
    )
    assert symbols(classify(frame, cfg()).buy) == ["DDD", "BBB", "AAA", "CCC"]


def test_input_order_does_not_change_lists():
    rows = [
        buyable(f"S{i}", 70 + (i % 3), market_cap=(i % 4 + 1) * 1e9, sector=f"Sec{i % 5}")
        for i in range(20)
    ]
    forward = classify(pd.DataFrame(rows), cfg())
    backward = classify(pd.DataFrame(rows[::-1]), cfg())
    assert symbols(forward.buy) == symbols(backward.buy)


def test_nan_rows_are_skipped():
    frame = pd.DataFrame(
        [
            buyable("OK", 80),
            buyable("NOSCORE", float("nan")),
            buyable("NORSI", 99, rsi=float("nan")),
        ]
    )
    assert symbols(classify(frame, cfg()).buy) == ["OK"]


def test_sector_cap_applies_to_buy_only():
    tech = [buyable(f"T{i}", 90 - i, sector="Tech") for i in range(5)]
    other = [buyable("O1", 50, sector="Energy")]
    lists = classify(pd.DataFrame(tech + other), cfg(max_per_sector=3))
    assert symbols(lists.buy) == ["T0", "T1", "T2", "O1"]


def test_extended_high_scorer_goes_to_hold_not_buy():
    # RSI 72 passes the BUY RSI filter (< 75) but is "extended" (> 70) -> HOLD only
    frame = pd.DataFrame([buyable("HOT", 95, rsi=72.0), buyable("CALM", 80)])
    lists = classify(frame, cfg())
    assert symbols(lists.hold) == ["HOT"]
    assert symbols(lists.buy) == ["CALM"]


def test_far_above_sma50_is_extended():
    # 20% above SMA50 with RSI 60 -> extended (threshold 15%) -> HOLD
    frame = pd.DataFrame([buyable("FAR", 90, reference_close=120.0, sma50=100.0, sma200=90.0)])
    lists = classify(frame, cfg())
    assert symbols(lists.hold) == ["FAR"] and lists.buy.empty


def test_hold_requires_uptrend_and_min_score():
    frame = pd.DataFrame(
        [
            buyable("LOWSCORE", 55, rsi=72.0),  # extended but score < 60
            buyable("NOTREND", 90, rsi=72.0, sma50=85.0, sma200=90.0),  # SMA50 < SMA200
        ]
    )
    assert classify(frame, cfg()).hold.empty


def test_each_stock_in_at_most_one_list(universe_raw):
    frame = universe_raw.dropna(subset=["sma200", "adx", "macd"]).assign(
        reference_close=lambda d: d["close"]
    )
    lists = classify(score_frame(frame, Weights()), cfg(n_buy=50, n_sell=50, n_hold=50))
    all_symbols = (
        lists.buy["symbol"].tolist() + lists.sell["symbol"].tolist() + lists.hold["symbol"].tolist()
    )
    assert len(all_symbols) == len(set(all_symbols))


def test_sell_lowest_scores_first_with_filters():
    frame = pd.DataFrame(
        [
            sellable("S1", 20, market_cap=2e9),
            sellable("S2", 10),
            sellable("S3", 20, market_cap=8e9),
            sellable("NOTSELL", 5, rec_all=0.0),  # rating not weak enough
            buyable("ABOVE", 1),  # above SMA200 -> never SELL
        ]
    )
    assert symbols(classify(frame, cfg()).sell) == ["S2", "S3", "S1"]


def test_list_sizes_respected_and_ranked():
    frame = pd.DataFrame([buyable(f"B{i}", 90 - i, sector=f"S{i}") for i in range(15)])
    buy = classify(frame, cfg(n_buy=4)).buy
    assert symbols(buy) == ["B0", "B1", "B2", "B3"]
    assert buy["rank"].tolist() == [1, 2, 3, 4]


def test_buy_filters():
    frame = pd.DataFrame(
        [
            buyable("WEAKRATING", 99, rec_all=0.05),
            buyable("RSI75", 99, rsi=75.0),  # RSI must be < 75 (also extended -> not BUY anyway)
            buyable("BELOW200", 99, reference_close=85.0, sma50=80.0, sma200=90.0),
            buyable("GOOD", 50),
        ]
    )
    assert symbols(classify(frame, cfg()).buy) == ["GOOD"]
