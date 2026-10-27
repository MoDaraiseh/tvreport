# Methodology

tvreport is a transparent, deterministic, rule-based technical screen. There are no LLM calls and no randomness: the same input data always produces the same scores, lists and report. Every number here is also documented in the code (`src/tvreport/scoring.py`, `classify.py`, `weekly.py`, `quality.py`, `performance.py`).

## Data source

TradingView has no official public API. tvreport uses the [`tradingview-screener`](https://pypi.org/project/tradingview-screener/) package (3.2.2), which queries TradingView's scanner endpoint (`scanner.tradingview.com/america/scan`). It does not scrape rendered tradingview.com pages. All access goes through the `DataProvider` interface (`src/tvreport/providers/base.py`), so another provider can be added later without touching anything above it.

The following were verified live on 2026-10-27 with `scripts/discover_fields.py`, and the raw responses are saved in `tests/fixtures/`.

- **All target fields exist:** name, description, close, change, volume, average_volume_10d_calc, relative_volume_10d_calc, market_cap_basic, sector, industry, exchange, type, subtype, Recommend.All/MA/Other, RSI, MACD.macd, MACD.signal, ADX, SMA20/50/200, EMA20, Perf.W/1M/3M/YTD, price_52_week_high/low.
  - The pre-market fields are `premarket_change` (%), `premarket_change_abs`, `premarket_close` and `premarket_volume`.
- **Unknown field names are not rejected.** The endpoint returns `null` instead, even though the library sends `ignore_unknown_fields: False`. The discovery script therefore treats "null for every sample ticker" as "field missing".
- **Units:**
  - `change` is a percentage. The script checks that it equals `100 × (close / close[1] − 1)`.
  - `Perf.*` and `premarket_change` are percentages.
  - `Recommend.*` lies in −1..1, and `RSI` in 0..100.
- **Freshness:** the data is 15-minute delayed (`update_mode = delayed_streaming_900`). `time` and `time|1W` are the opening timestamps of the current daily and weekly bars, in epoch seconds.
- **`Perf.W` is not a calendar-week change.** Checked against independent daily closes, its implied reference was the *previous day's* close for JPM and the S&P 500, and matched no recent close for AAPL. It is fetched but never used for weekly figures. See [Weekly changes](#weekly-changes).
- **`close[1]|1W`** (the previous weekly bar's close) matched last Friday's independent close to the cent for AAPL, JPM and the S&P 500.
- **Indices** are only returned when the library's default stock-type filter is removed.
  - Symbols used: `SP:SPX`, `NASDAQ:IXIC`, `NASDAQ:NDX`, `DJ:DJI`, `FTSE:RUT` (the official Russell 2000) and `CBOE:VIX`.
  - `FTSE:RUT` does not update during the session: it showed the previous close as today's close. Its weekly reference matched the official close exactly.
  - When `FTSE:RUT` does not yet reflect the expected session, the report uses `TVC:RUT`, TradingView's real-time calculation of the index, and **says so next to the number**.

## Universe

US common stocks with a primary listing on NYSE, NASDAQ or AMEX, with market cap ≥ $2B, 10-day average volume ≥ 1M shares and price ≥ $5. All thresholds are in `config.yaml`. The filter is applied server-side in one request, `type = stock` AND `typespecs` has `common`. As verified, this excludes:

- **ADRs**, which are `type = dr` (e.g. BABA, ARM, PDD);
- **closed-end funds and trusts**, which are `type = fund`;
- **ETFs**, which have no `market_cap_basic` and therefore never pass the market-cap filter;
- **preferreds and warrants**, which are not `common`;
- **pre-IPO listings**, via `typespecs` `pre-ipo`.

**REITs** are classified by TradingView as common stock, so they are included. **US-listed ordinary shares of companies domiciled abroad** (Linde, Accenture, Medtronic, Shopify and others) are also common stock rather than ADRs, so they are included. Set `universe.include_foreign_domiciled: false` to keep `country = United States` only.

On 2026-10-27 the universe was 1,073–1,075 stocks. The run aborts if it is outside 200–3,000, which would indicate a broken filter.

## Data-quality gates

The run fails loudly rather than send a misleading report:

- **Rows are dropped** (and counted) if they are missing a field the score needs, have out-of-range values (RSI outside 0..100, Recommend.* outside −1..1, non-positive price, market cap or SMA, 52-week high below the low), or come from a different session than the rest.
  - Recent listings with fewer than ~200 sessions of history have no SMA200 and are excluded this way. That was 24 stocks on 2026-10-27, and the report says so.
- **More than 10% dropped** aborts the run.
- **Staleness:** each row's daily-bar date (`time`, in New York time) must match the session the NYSE calendar says this run should see.
  - **Monday 08:00:** the previous session. If TradingView has already opened today's pre-market daily bar, that is also accepted. The reference price then becomes `close[1]` (the previous session's close), and the volume sub-score is neutral for that run, because no same-session volume signal exists.
  - **Friday after the close:** today's session, or the last session if today is a holiday.

## Composite score (0–100)

Each sub-score is 0–100. The weights are in `config.yaml` and must sum to 1.0, which is checked at startup.

| Sub-score | Weight | Formula |
|---|---:|---|
| TV rating | 0.30 | `50 × (Recommend.All + 1)` |
| Trend | 0.25 | `25·[close > SMA50] + 25·[close > SMA200] + 25·[SMA50 > SMA200]`, plus ADX points: `25 × clip((ADX − 15)/25, 0, 1)` in an aligned uptrend (close > SMA50 > SMA200), `0` in an aligned downtrend (close < SMA50 < SMA200), `12.5` otherwise |
| Momentum | 0.25 | `0.4·RSIcurve + 0.2·MACDstate + 0.2·pct(Perf.1M) + 0.2·pct(Perf.3M)` |
| Volume | 0.10 | `pct(sign(change%) × min(relative_volume_10d, 3))` |
| 52-week range | 0.10 | `100 × (close − low52)/(high52 − low52)`, clipped to 0..100; 50 if high52 = low52 |

- **RSIcurve** is piecewise linear through (30, 0), (50, 80), (55, 100), (65, 100), (70, 70), (80, 0), and 0 below 30 and above 80. It rewards healthy momentum and penalises weak (< 30) and overbought (> 70) readings.
- **MACDstate** is 100 if MACD > signal and MACD > 0; 70 if MACD > signal and MACD ≤ 0; 30 if MACD ≤ signal and MACD > 0; 0 otherwise.
- **pct()** is a cross-sectional mid-rank percentile against the universe, `100 × (#{u < v} + 0.5·#{u = v}) / n`, so outliers can't dominate. Holdings outside the universe are ranked against the universe's distribution.
- **Prices** used in the comparisons are the reference close: the latest completed session's close.

## Classification

- **Definitions:** "extended" means RSI > 70, or close more than 15% above SMA50. "Uptrend" means close > SMA200 and SMA50 > SMA200. Both thresholds are configurable.
- **BUY:** the highest scores with close > SMA200, Recommend.All ≥ 0.1, RSI < 75 and **not** extended. At most 3 per sector.
- **SELL:** the lowest scores with close < SMA200 and Recommend.All ≤ −0.1.
- **HOLD:** the highest scores that are extended, in an uptrend, and have score ≥ 60. It means "if you own it, keep it; don't buy more here."
- **List size:** N = 10 per list (configurable).
- **One list per stock:** SELL requires close < SMA200 and BUY/HOLD require close > SMA200, and extended stocks can only be HOLD. The lists are also filled SELL → HOLD → BUY, skipping stocks already listed.
- **Ties** are broken by market cap (descending), then ticker (ascending).
- **Rows with a missing field** that a rule needs are skipped.

**Portfolio mode** (if `holdings.yaml` or the `HOLDINGS_YAML` secret exists): score ≥ 65 means BUY MORE, < 40 means SELL, otherwise HOLD (configurable). P/L is shown when an average cost is given. A holding the provider can't return, or that lacks data, is listed as N/A with the reason.

## Weekly changes

"Weekly" always means the **calendar week**, computed from TradingView's weekly bars as `close / close[1]|1W − 1`, never from `Perf.W`.

- **Friday report:** the current week's bar, Monday to the latest close.
- **Monday report:** the week just ended. If TradingView has already started this week's bar before the open, last week's change can't be read (`close[2]|1W` is not available). It is then taken from the values the previous Friday report stored in `history.db`. If none are stored, the report says it is unavailable rather than estimating.

## Performance tracking

- **Reference price** for a Monday pick is the latest completed session's close available at the Monday run (normally the previous Friday's close), stored in `data/history.db`.
- **Return:** on Friday, `return = latest close / reference − 1`.
- **Benchmark:** the S&P 500 over the same window, from its level at the Monday run to its level now.
- **Hit rate:** a BUY counts as a hit if it beat the S&P 500; a SELL counts as a hit if it lagged the S&P 500. "Right direction" (BUY up, SELL down) is shown alongside.
- **Cumulative statistics** cover every recorded Friday evaluation.

## Known limitations

- **Delayed data:** TradingView scanner data is delayed (15 minutes for these symbols).
- **Monday pre-market behaviour not yet observed:** how TradingView's daily and weekly bars look at 08:00 ET on a Monday couldn't be observed when this was built (on a Tuesday). The code handles both "still last session/week" and "already rolled", and fails loudly on anything else. `scripts/check_session_bars.py` shows which case applies.
- **`FTSE:RUT` publishes late:** it can lag after the close, in which case the report shows `TVC:RUT` and labels it.
- **Sectors** are TradingView's sector classification (e.g. "Electronic Technology"), not GICS.
- **A technical screen only:** no fundamentals, news or earnings dates are considered. Not financial advice.
