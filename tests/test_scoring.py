"""Scoring math against hand-calculated values."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from tests.conftest import stock_row
from tvreport.config import Weights
from tvreport.scoring import (
    macd_state,
    percentile_against,
    range_52w_score,
    rationale,
    rsi_curve,
    score_frame,
    trend_score,
    tv_rating_label,
    tv_rating_score,
    volume_signal,
)

S = pd.Series


def test_tv_rating_score_maps_minus1_1_to_0_100():
    assert tv_rating_score(S([-1.0, -0.5, 0.0, 0.5, 1.0])).tolist() == [0, 25, 50, 75, 100]


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [
        (20, 0),
        (30, 0),
        (40, 40),
        (50, 80),
        (52.5, 90),
        (60, 100),
        (65, 100),
        (67.5, 85),
        (70, 70),
        (75, 35),
        (80, 0),
        (90, 0),
    ],
)
def test_rsi_curve_points(rsi, expected):
    assert rsi_curve(S([float(rsi)])).iloc[0] == pytest.approx(expected)


def test_macd_state_four_cases():
    macd = S([2.0, -1.0, 1.0, -2.0])
    signal = S([1.0, -2.0, 2.0, -1.0])
    assert macd_state(macd, signal).tolist() == [100.0, 70.0, 30.0, 0.0]


def test_trend_score_cases():
    # uptrend: 25*3 + 25*clip((30-15)/25) = 75 + 15 = 90
    # downtrend: close 80 < sma50 90 < sma200 100 -> 0 + 0 = 0
    # mixed: close 95 < sma50 100, but > sma200 90 and sma50 > sma200 -> 50 + 12.5 = 62.5
    # strong uptrend: ADX 60 -> clip(1.8) = 1 -> 75 + 25 = 100
    close = S([110.0, 80.0, 95.0, 110.0])
    sma50 = S([100.0, 90.0, 100.0, 100.0])
    sma200 = S([90.0, 100.0, 90.0, 90.0])
    adx = S([30.0, 40.0, 20.0, 60.0])
    assert trend_score(close, sma50, sma200, adx).tolist() == pytest.approx([90, 0, 62.5, 100])


def test_percentile_mid_rank_with_ties():
    ref = S([1.0, 1.0, 2.0])
    # 1: (0 + 0.5*2)/3 = 33.33 ; 2: (2 + 0.5*1)/3 = 83.33 ; 0: 0 ; 3: 100
    got = percentile_against(S([1.0, 2.0, 0.0, 3.0]), ref).tolist()
    assert got == pytest.approx([100 / 3, 250 / 3, 0, 100])


def test_percentile_nan_stays_nan():
    got = percentile_against(S([float("nan"), 1.0]), S([1.0, 2.0]))
    assert math.isnan(got.iloc[0]) and got.iloc[1] == pytest.approx(25.0)


def test_range_52w_score():
    got = range_52w_score(
        S([110.0, 70.0, 130.0, 50.0]), S([80.0, 80.0, 80.0, 50.0]), S([120.0, 120.0, 120.0, 50.0])
    )
    assert got.tolist() == pytest.approx([75.0, 0.0, 100.0, 50.0])  # last: high == low


def test_volume_signal_signed_and_capped():
    got = volume_signal(S([1.0, -2.0, 0.0, 3.0]), S([2.0, 1.5, 5.0, 9.0]))
    assert got.tolist() == [2.0, -1.5, 0.0, 3.0]


def _three_stock_frame() -> pd.DataFrame:
    a = stock_row(
        symbol="X:A",
        ticker="A",
        rec_all=0.5,
        reference_close=110.0,
        close=110.0,
        sma50=100.0,
        sma200=90.0,
        adx=30.0,
        rsi=60.0,
        macd=2.0,
        macd_signal=1.0,
        perf_1m=10.0,
        perf_3m=10.0,
        change_pct=1.0,
        rel_volume_10d=2.0,
        low_52w=80.0,
        high_52w=120.0,
    )
    b = stock_row(
        symbol="X:B",
        ticker="B",
        rec_all=0.0,
        reference_close=100.0,
        close=100.0,
        sma50=100.0,
        sma200=100.0,
        adx=10.0,
        rsi=50.0,
        macd=0.0,
        macd_signal=0.0,
        perf_1m=0.0,
        perf_3m=0.0,
        change_pct=-1.0,
        rel_volume_10d=1.5,
        low_52w=50.0,
        high_52w=150.0,
    )
    c = stock_row(
        symbol="X:C",
        ticker="C",
        rec_all=-1.0,
        reference_close=80.0,
        close=80.0,
        sma50=90.0,
        sma200=100.0,
        adx=40.0,
        rsi=25.0,
        macd=-2.0,
        macd_signal=-1.0,
        perf_1m=-10.0,
        perf_3m=-10.0,
        change_pct=0.0,
        rel_volume_10d=5.0,
        low_52w=80.0,
        high_52w=160.0,
    )
    return pd.DataFrame([a, b, c])


def test_composite_hand_calculated():
    out = score_frame(_three_stock_frame(), Weights())
    a, b, c = (out.iloc[i] for i in range(3))

    # A: tv 75; trend 90; momentum 0.4*100 + 0.2*100 + 0.2*83.333 + 0.2*83.333 = 93.333
    #    volume: signals [2, -1.5, 0] -> A = (2 + 0.5)/3 = 83.333 ; range (110-80)/40 = 75
    #    composite = .3*75 + .25*90 + .25*93.333 + .1*83.333 + .1*75 = 84.1667
    assert a["s_tv"] == pytest.approx(75)
    assert a["s_trend"] == pytest.approx(90)
    assert a["s_momentum"] == pytest.approx(93.3333, abs=1e-3)
    assert a["s_volume"] == pytest.approx(83.3333, abs=1e-3)
    assert a["s_range"] == pytest.approx(75)
    assert a["score"] == pytest.approx(84.1667, abs=1e-3)

    # B: tv 50; trend: close==sma50==sma200 -> no points, mixed -> 12.5
    #    momentum 0.4*80 + 0.2*0 (macd == signal, not above; macd 0 not > 0) + 0.2*50 + 0.2*50 = 52
    #    volume -1.5 -> (0 + 0.5)/3 = 16.667 ; range 50/100 = 50
    #    composite = 15 + 3.125 + 13 + 1.6667 + 5 = 37.7917
    assert b["s_trend"] == pytest.approx(12.5)
    assert b["s_momentum"] == pytest.approx(52)
    assert b["score"] == pytest.approx(37.7917, abs=1e-3)

    # C: tv 0; trend 0 (aligned downtrend); momentum 0.4*0 + 0 + 0.2*16.667*2 = 6.667
    #    volume 0 -> (1 + 0.5)/3 = 50 ; range 0
    #    composite = 0 + 0 + 1.6667 + 5 + 0 = 6.6667
    assert c["s_trend"] == pytest.approx(0)
    assert c["s_volume"] == pytest.approx(50)
    assert c["score"] == pytest.approx(6.6667, abs=1e-3)


def test_volume_neutral_when_unavailable():
    out = score_frame(_three_stock_frame(), Weights(), volume_available=False)
    assert out["s_volume"].tolist() == [50.0, 50.0, 50.0]


def test_scores_are_deterministic_and_order_independent():
    frame = _three_stock_frame()
    first = score_frame(frame, Weights()).set_index("symbol")["score"]
    shuffled = score_frame(frame.iloc[[2, 0, 1]].reset_index(drop=True), Weights())
    assert (
        shuffled.set_index("symbol")["score"].sort_index().tolist() == first.sort_index().tolist()
    )


def test_outside_universe_ranked_against_reference():
    reference = _three_stock_frame()
    holding = pd.DataFrame([stock_row(symbol="X:H", ticker="H", perf_1m=5.0, perf_3m=5.0)])
    out = score_frame(holding, Weights(), reference=reference)
    # 5 within [-10, 0, 10]: (2 + 0)/3 -> 66.667 for both ranks
    assert out["pct_1m"].iloc[0] == pytest.approx(200 / 3)


def test_scores_within_0_100_on_real_universe(universe_raw):
    frame = universe_raw.dropna(
        subset=[
            "rec_all",
            "rsi",
            "sma50",
            "sma200",
            "adx",
            "macd",
            "macd_signal",
            "perf_1m",
            "perf_3m",
            "low_52w",
            "high_52w",
        ]
    )
    frame = frame.assign(reference_close=frame["close"])
    out = score_frame(frame, Weights())
    for column in ("s_tv", "s_trend", "s_momentum", "s_volume", "s_range", "score"):
        assert out[column].between(0, 100).all(), column


def test_rationale_and_labels():
    row = pd.Series(stock_row(rec_all=0.6, rsi=58.4, rel_volume_10d=1.8, perf_1m=4.21))
    text = rationale(row)
    assert text == (
        "Above 50/200-day SMAs · TV rating Strong Buy · RSI 58 · MACD above signal · "
        "volume 1.8× average · 1M +4.2%"
    )
    assert "volume" not in rationale(row, include_volume=False)
    assert [tv_rating_label(v) for v in (0.5, 0.1, 0.0, -0.1, -0.5)] == [
        "Strong Buy",
        "Buy",
        "Neutral",
        "Sell",
        "Strong Sell",
    ]
