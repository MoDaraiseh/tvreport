"""Composite 0-100 score per stock from five sub-scores (each 0-100).

Deterministic and rule-based: same input rows -> same scores. Price-based
comparisons use `reference_close` (the latest completed session's close; see
quality.py). Percentile ranks are cross-sectional, against the universe:

    pct(v) = 100 * (#{u < v} + 0.5 * #{u == v}) / n      (mid-rank for ties)

so outliers can't dominate. Stocks outside the universe (portfolio holdings)
are ranked against the universe's distribution with the same formula.

Sub-scores
----------
tv_rating   = 50 * (Recommend.All + 1)                          (-1..1 -> 0..100)
trend       = 25*[close > SMA50] + 25*[close > SMA200] + 25*[SMA50 > SMA200]
              + ADX points: 25 * clip((ADX - 15) / 25, 0, 1) in an aligned uptrend
                (close > SMA50 > SMA200); 0 in an aligned downtrend
                (close < SMA50 < SMA200); 12.5 otherwise
momentum    = 0.4*RSI_curve + 0.2*MACD_state + 0.2*pct(Perf.1M) + 0.2*pct(Perf.3M)
              RSI_curve: piecewise linear through (30,0) (50,80) (55,100) (65,100)
                (70,70) (80,0); 0 below 30 and above 80 -- rewards healthy momentum,
                penalises weak (<30) and overbought (>70)
              MACD_state: 100 if MACD > signal and MACD > 0; 70 if MACD > signal and
                MACD <= 0; 30 if MACD <= signal and MACD > 0; 0 otherwise
volume      = pct(sign(change%) * min(relative_volume_10d, 3))  -- heavy volume on an
              up day ranks high, on a down day low. 50 for everyone when no
              same-session volume signal exists (a Monday pre-market daily bar).
range_52w   = 100 * (close - low52) / (high52 - low52), clipped to 0..100
              (50 if high52 == low52)

composite   = sum(weight_i * subscore_i), weights from config.yaml (sum to 1.0)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Weights

RSI_CURVE_X = (30.0, 50.0, 55.0, 65.0, 70.0, 80.0)
RSI_CURVE_Y = (0.0, 80.0, 100.0, 100.0, 70.0, 0.0)
SUBSCORES = ("s_tv", "s_trend", "s_momentum", "s_volume", "s_range")


def percentile_against(values: pd.Series, reference: pd.Series) -> pd.Series:
    """Mid-rank percentile (0-100) of each value within `reference` (NaNs ignored)."""

    ref = np.sort(reference.dropna().to_numpy(dtype=float))
    if ref.size == 0:
        return pd.Series(np.nan, index=values.index, dtype=float)
    vals = values.to_numpy(dtype=float)
    left = np.searchsorted(ref, vals, side="left")
    right = np.searchsorted(ref, vals, side="right")
    pct = 100.0 * (left + 0.5 * (right - left)) / ref.size
    pct[np.isnan(vals)] = np.nan
    return pd.Series(pct, index=values.index, dtype=float)


def tv_rating_score(rec_all: pd.Series) -> pd.Series:
    return (50.0 * (rec_all.astype(float) + 1.0)).clip(0, 100)


def trend_score(close: pd.Series, sma50: pd.Series, sma200: pd.Series, adx: pd.Series) -> pd.Series:
    above50, above200, golden = close > sma50, close > sma200, sma50 > sma200
    points = 25.0 * (above50.astype(float) + above200.astype(float) + golden.astype(float))
    strength = ((adx.astype(float) - 15.0) / 25.0).clip(0, 1)
    up = above50 & golden
    down = (close < sma50) & (sma50 < sma200)
    adx_points = np.where(up, 25.0 * strength, np.where(down, 0.0, 12.5))
    return (points + adx_points).astype(float)


def rsi_curve(rsi: pd.Series) -> pd.Series:
    return pd.Series(np.interp(rsi.astype(float), RSI_CURVE_X, RSI_CURVE_Y), index=rsi.index)


def macd_state(macd: pd.Series, signal: pd.Series) -> pd.Series:
    above = macd > signal
    positive = macd > 0
    out = np.select(
        [above & positive, above & ~positive, ~above & positive], [100.0, 70.0, 30.0], 0.0
    )
    return pd.Series(out, index=macd.index, dtype=float)


def volume_signal(change_pct: pd.Series, rel_volume: pd.Series) -> pd.Series:
    signal = np.sign(change_pct.astype(float)) * rel_volume.astype(float).clip(upper=3.0)
    return pd.Series(signal, index=change_pct.index, dtype=float)


def range_52w_score(close: pd.Series, low52: pd.Series, high52: pd.Series) -> pd.Series:
    span = high52 - low52
    raw = 100.0 * (close - low52) / span.where(span != 0)
    return raw.clip(0, 100).where(span != 0, 50.0).astype(float)


def score_frame(
    frame: pd.DataFrame,
    weights: Weights,
    reference: pd.DataFrame | None = None,
    *,
    volume_available: bool = True,
) -> pd.DataFrame:
    """Return a copy of `frame` with s_tv, s_trend, s_momentum, s_volume,
    s_range, score (and rsi_curve, macd_state helpers) added. `reference` is
    the universe used for percentile ranks (defaults to `frame` itself)."""

    ref = frame if reference is None else reference
    out = frame.copy()
    close = out["reference_close"].astype(float)

    out["s_tv"] = tv_rating_score(out["rec_all"])
    out["s_trend"] = trend_score(close, out["sma50"], out["sma200"], out["adx"])
    out["rsi_curve"] = rsi_curve(out["rsi"])
    out["macd_state"] = macd_state(out["macd"], out["macd_signal"])
    out["pct_1m"] = percentile_against(out["perf_1m"], ref["perf_1m"])
    out["pct_3m"] = percentile_against(out["perf_3m"], ref["perf_3m"])
    out["s_momentum"] = (
        0.4 * out["rsi_curve"] + 0.2 * out["macd_state"] + 0.2 * out["pct_1m"] + 0.2 * out["pct_3m"]
    )
    if volume_available:
        out["s_volume"] = percentile_against(
            volume_signal(out["change_pct"], out["rel_volume_10d"]),
            volume_signal(ref["change_pct"], ref["rel_volume_10d"]),
        )
    else:
        out["s_volume"] = 50.0
    out["s_range"] = range_52w_score(close, out["low_52w"], out["high_52w"])
    out["score"] = (
        weights.tv_rating * out["s_tv"]
        + weights.trend * out["s_trend"]
        + weights.momentum * out["s_momentum"]
        + weights.volume * out["s_volume"]
        + weights.range_52w * out["s_range"]
    )
    return out


def tv_rating_label(rec_all: float) -> str:
    """TradingView's own rating bands for Recommend.All."""

    if rec_all >= 0.5:
        return "Strong Buy"
    if rec_all >= 0.1:
        return "Buy"
    if rec_all > -0.1:
        return "Neutral"
    if rec_all > -0.5:
        return "Sell"
    return "Strong Sell"


def rationale(row: pd.Series, *, include_volume: bool = True) -> str:
    """One plain-English line built from the row's actual values. Relative volume
    is left out when it isn't a same-session signal (see score_frame)."""

    close, sma50, sma200 = row["reference_close"], row["sma50"], row["sma200"]
    if close > sma50 and close > sma200:
        trend = "Above 50/200-day SMAs"
    elif close < sma50 and close < sma200:
        trend = "Below 50/200-day SMAs"
    elif close > sma200:
        trend = "Above 200-day, below 50-day SMA"
    else:
        trend = "Above 50-day, below 200-day SMA"
    parts = [
        trend,
        f"TV rating {tv_rating_label(float(row['rec_all']))}",
        f"RSI {row['rsi']:.0f}",
        "MACD above signal" if row["macd"] > row["macd_signal"] else "MACD below signal",
    ]
    if include_volume and pd.notna(row.get("rel_volume_10d")):
        parts.append(f"volume {row['rel_volume_10d']:.1f}× average")
    if pd.notna(row.get("perf_1m")):
        parts.append(f"1M {row['perf_1m']:+.1f}%")
    return " · ".join(parts)
