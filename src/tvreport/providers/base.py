"""Provider-neutral data access interface.

Everything above this layer works with DataFrames using the *internal* column
names below, never a provider's own field names, so another provider (e.g.
yfinance) can be swapped in by implementing `DataProvider` alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import pandas as pd

from ..config import UniverseConfig

# Internal column names for one stock row.
STOCK_COLUMNS: tuple[str, ...] = (
    "symbol",  # exchange-qualified, e.g. "NASDAQ:AAPL"
    "ticker",  # bare, e.g. "AAPL"
    "description",
    "close",
    "change_pct",  # % change of the current daily bar vs previous close
    "volume",
    "avg_volume_10d",
    "rel_volume_10d",
    "market_cap",
    "sector",
    "industry",
    "exchange",
    "type",
    "subtype",
    "country",
    "rec_all",
    "rec_ma",
    "rec_other",
    "rsi",
    "macd",
    "macd_signal",
    "adx",
    "sma20",
    "sma50",
    "sma200",
    "ema20",
    "perf_w",
    "perf_1m",
    "perf_3m",
    "perf_ytd",
    "high_52w",
    "low_52w",
    "premarket_change_pct",
    "premarket_close",
    "prev_close",  # close of the previous daily bar
    "bar_time",  # epoch seconds: open of the current daily bar
    "week_bar_time",  # epoch seconds: open of the current weekly bar
    "week_close",  # close of the current weekly bar
    "prev_week_close",  # close of the previous weekly bar
    "update_mode",
    "current_session",
)

INDEX_COLUMNS: tuple[str, ...] = (
    "symbol",
    "description",
    "type",
    "close",
    "change_pct",
    "prev_close",
    "bar_time",
    "week_bar_time",
    "week_close",
    "prev_week_close",
    "update_mode",
)


class ProviderError(RuntimeError):
    """Data could not be fetched or did not have the expected shape."""


class DataProvider(ABC):
    """Source of market data. Implementations return DataFrames with exactly
    STOCK_COLUMNS / INDEX_COLUMNS (missing values as NaN/None)."""

    @abstractmethod
    def fetch_universe(self, cfg: UniverseConfig) -> pd.DataFrame:
        """All stocks passing the universe filters, one row per primary listing."""

    @abstractmethod
    def fetch_stocks(self, tickers: Sequence[str]) -> pd.DataFrame:
        """Specific stocks (bare tickers like "AAPL" or "NYSE:AAPL"), regardless
        of universe filters. Unknown tickers are simply absent from the result."""

    @abstractmethod
    def fetch_indices(self, symbols: Sequence[str]) -> pd.DataFrame:
        """Index rows for the given provider symbols."""
