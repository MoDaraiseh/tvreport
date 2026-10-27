"""TradingView provider, via the `tradingview-screener` package (scanner endpoint).

Field names, units and symbols here were verified live on 2026-10-27 with
tradingview-screener 3.2.2 (see scripts/discover_fields.py and METHODOLOGY.md):

* `change`, `Perf.*`, `premarket_change` are percentages; `Recommend.*` is -1..1.
* `time` / `time|1W` are epoch seconds of the current daily / weekly bar's open.
* `close[1]|1W` is the previous weekly bar's close (matched independent daily
  closes to the cent). `Perf.W` is NOT a calendar-week change and is not used
  for weekly figures.
* Unknown field names are not rejected by the endpoint -- they silently come
  back as null -- so `discover_fields.py` treats "null for every sample" as missing.
* The library's default query restricts results to stock/dr/fund types, which
  hides indices; index and ad-hoc lookups remove that default filter.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd
import requests
from tradingview_screener.column import col
from tradingview_screener.query import Query

from ..config import ProviderConfig, UniverseConfig
from .base import INDEX_COLUMNS, STOCK_COLUMNS, DataProvider, ProviderError

log = logging.getLogger(__name__)

# internal name -> TradingView scanner field
STOCK_FIELDS: dict[str, str] = {
    "ticker": "name",
    "description": "description",
    "close": "close",
    "change_pct": "change",
    "volume": "volume",
    "avg_volume_10d": "average_volume_10d_calc",
    "rel_volume_10d": "relative_volume_10d_calc",
    "market_cap": "market_cap_basic",
    "sector": "sector",
    "industry": "industry",
    "exchange": "exchange",
    "type": "type",
    "subtype": "subtype",
    "country": "country",
    "rec_all": "Recommend.All",
    "rec_ma": "Recommend.MA",
    "rec_other": "Recommend.Other",
    "rsi": "RSI",
    "macd": "MACD.macd",
    "macd_signal": "MACD.signal",
    "adx": "ADX",
    "sma20": "SMA20",
    "sma50": "SMA50",
    "sma200": "SMA200",
    "ema20": "EMA20",
    "perf_w": "Perf.W",
    "perf_1m": "Perf.1M",
    "perf_3m": "Perf.3M",
    "perf_ytd": "Perf.YTD",
    "high_52w": "price_52_week_high",
    "low_52w": "price_52_week_low",
    "premarket_change_pct": "premarket_change",
    "premarket_close": "premarket_close",
    "prev_close": "close[1]",
    "bar_time": "time",
    "week_bar_time": "time|1W",
    "week_close": "close|1W",
    "prev_week_close": "close[1]|1W",
    "update_mode": "update_mode",
    "current_session": "current_session",
}

INDEX_FIELDS: dict[str, str] = {
    "description": "description",
    "type": "type",
    "close": "close",
    "change_pct": "change",
    "prev_close": "close[1]",
    "bar_time": "time",
    "week_bar_time": "time|1W",
    "week_close": "close|1W",
    "prev_week_close": "close[1]|1W",
    "update_mode": "update_mode",
}

_TEXT_COLUMNS = {
    "symbol",
    "ticker",
    "description",
    "sector",
    "industry",
    "exchange",
    "type",
    "subtype",
    "country",
    "update_mode",
    "current_session",
}

UNIVERSE_LIMIT = 5000
"""Rows requested for the universe in one call; a totalCount above this aborts."""

Transport = Callable[[Query, float], Mapping[str, Any]]


def _to_frame(
    payload: Mapping[str, Any], fields: Mapping[str, str], columns: Sequence[str]
) -> pd.DataFrame:
    names = list(fields)
    rows = []
    for item in payload["data"]:
        values = item["d"]
        if len(values) != len(names):
            raise ProviderError(
                f"row {item.get('s')!r} has {len(values)} values, expected {len(names)}"
            )
        rows.append({"symbol": item["s"], **dict(zip(names, values, strict=True))})
    frame = pd.DataFrame(rows, columns=["symbol", *names])
    for column in frame.columns:
        if column not in _TEXT_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[list(columns)].reset_index(drop=True)


def parse_stocks(payload: Mapping[str, Any]) -> pd.DataFrame:
    """Raw scanner payload (columns in STOCK_FIELDS order) -> internal frame."""

    return _to_frame(payload, STOCK_FIELDS, STOCK_COLUMNS)


def parse_indices(payload: Mapping[str, Any]) -> pd.DataFrame:
    """Raw scanner payload (columns in INDEX_FIELDS order) -> internal frame."""

    return _to_frame(payload, INDEX_FIELDS, INDEX_COLUMNS)


def without_default_filters(query: Query) -> Query:
    """Drop the library's default `filter` (is_primary) and `filter2` (only
    stock/dr/fund types) -- needed for indices and explicit ticker lookups."""

    query.query.pop("filter", None)
    query.query.pop("filter2", None)
    return query


def universe_query(cfg: UniverseConfig) -> Query:
    """One request for the whole filtered universe (US common stocks)."""

    conditions = [
        col("is_primary") == True,  # noqa: E712 - builds a filter expression
        col("exchange").isin(cfg.exchanges),
        col("type") == "stock",
        col("typespecs").has(["common"]),
        col("typespecs").has_none_of(["pre-ipo"]),
        col("market_cap_basic") >= cfg.min_market_cap,
        col("average_volume_10d_calc") >= cfg.min_avg_volume_10d,
        col("close") >= cfg.min_price,
    ]
    if not cfg.include_foreign_domiciled:
        conditions.append(col("country") == "United States")
    query = (
        Query("america")
        .select(*STOCK_FIELDS.values())
        .where(*conditions)
        .order_by("market_cap_basic", ascending=False)
        .limit(UNIVERSE_LIMIT)
    )
    query.query.pop("filter2", None)  # the explicit conditions replace the library default
    return query


def index_query(symbols: Sequence[str]) -> Query:
    return without_default_filters(
        Query().select(*INDEX_FIELDS.values()).set_tickers(*symbols)
    ).limit(len(symbols) + 10)


def ticker_query(symbols: Sequence[str]) -> Query:
    """Exchange-qualified symbols (e.g. NYSE:JPM), any security type."""

    return without_default_filters(
        Query().select(*STOCK_FIELDS.values()).set_tickers(*symbols)
    ).limit(len(symbols) + 10)


def _default_transport(query: Query, timeout: float) -> Mapping[str, Any]:
    return query.get_scanner_data_raw(timeout=timeout)


class TradingViewProvider(DataProvider):
    """Scanner-endpoint provider: throttled to one request per
    `min_interval_seconds`, explicit timeout, up to `max_attempts` attempts with
    exponential backoff on network errors and HTTP 5xx; never retries a 4xx."""

    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        transport: Transport = _default_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.request_count = 0

    # ------------------------------------------------------------------ HTTP
    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._cfg.min_interval_seconds - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def execute(self, query: Query, what: str) -> Mapping[str, Any]:
        """Run one scanner request under the throttle/retry policy."""

        attempts = self._cfg.max_attempts
        for attempt in range(1, attempts + 1):
            self._throttle()
            self.request_count += 1
            try:
                payload = self._transport(query, self._cfg.timeout_seconds)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and 400 <= status < 500:
                    raise ProviderError(f"{what}: HTTP {status} (not retried)") from exc
                error: Exception = exc
            except (requests.ConnectionError, requests.Timeout) as exc:
                error = exc
            else:
                if not isinstance(payload, Mapping) or "data" not in payload:
                    raise ProviderError(f"{what}: unexpected response shape")
                return payload
            if attempt == attempts:
                raise ProviderError(f"{what}: failed after {attempts} attempts: {error}") from error
            backoff = self._cfg.backoff_base_seconds * 2 ** (attempt - 1)
            log.warning(
                "%s: attempt %d/%d failed (%s); retrying in %.0fs",
                what,
                attempt,
                attempts,
                type(error).__name__,
                backoff,
            )
            self._sleep(backoff)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ interface
    def fetch_universe(self, cfg: UniverseConfig) -> pd.DataFrame:
        payload = self.execute(universe_query(cfg), "universe")
        total = int(payload.get("totalCount", 0))
        if total > len(payload["data"]):
            raise ProviderError(
                f"universe truncated: {total} matches but only {len(payload['data'])} returned"
            )
        return parse_stocks(payload)

    def fetch_stocks(self, tickers: Sequence[str]) -> pd.DataFrame:
        qualified = sorted({t.upper() for t in tickers if ":" in t})
        bare = sorted({t.upper() for t in tickers if ":" not in t})
        frames: list[pd.DataFrame] = []
        if qualified:
            query = ticker_query(qualified)
            frames.append(parse_stocks(self.execute(query, "stocks")))
        if bare:
            query = (
                Query("america")
                .select(*STOCK_FIELDS.values())
                .where(col("name").isin(bare), col("is_primary") == True)  # noqa: E712
                .limit(len(bare) * 3 + 10)
            )
            query.query.pop("filter2", None)
            frames.append(parse_stocks(self.execute(query, "stocks")))
        if not frames:
            return pd.DataFrame(columns=list(STOCK_COLUMNS))
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset="symbol").reset_index(drop=True)

    def fetch_indices(self, symbols: Sequence[str]) -> pd.DataFrame:
        query = index_query(symbols)
        frame = parse_indices(self.execute(query, "indices"))
        missing = sorted(set(symbols) - set(frame["symbol"]))
        if missing:
            raise ProviderError(f"indices not returned by TradingView: {', '.join(missing)}")
        return frame
