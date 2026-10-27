"""Shared fixtures. All network access is replaced by recorded responses
(tests/fixtures/*.json, recorded with scripts/discover_fields.py)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from tvreport.config import Config, UniverseConfig, load_config
from tvreport.providers.base import STOCK_COLUMNS, DataProvider
from tvreport.providers.tradingview import parse_indices, parse_stocks
from tvreport.schedule import NY, MarketCalendar

FIXTURES = Path(__file__).parent / "fixtures"


def load_payload(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    response: dict[str, Any] = data["response"]
    return response


def epoch(day: date, at: time = time(9, 30)) -> int:
    return int(datetime.combine(day, at, NY).timestamp())


def retime(frame: pd.DataFrame, session: date, week_start: date) -> pd.DataFrame:
    """Pretend the recorded rows come from `session` (daily bar) within the week
    starting `week_start` (weekly bar)."""

    out = frame.copy()
    present = out["bar_time"].notna()
    out.loc[present, "bar_time"] = epoch(session)
    out.loc[out["week_bar_time"].notna(), "week_bar_time"] = epoch(week_start)
    return out


class FakeProvider(DataProvider):
    def __init__(
        self,
        universe: pd.DataFrame,
        indices: pd.DataFrame,
        stocks: pd.DataFrame | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.universe = universe
        self.indices = indices
        self.stocks = stocks if stocks is not None else universe
        self.error = error
        self.calls: list[str] = []

    def fetch_universe(self, cfg: UniverseConfig) -> pd.DataFrame:
        self.calls.append("universe")
        if self.error:
            raise self.error
        return self.universe.copy()

    def fetch_stocks(self, tickers: Sequence[str]) -> pd.DataFrame:
        self.calls.append("stocks")
        wanted = {t.upper() for t in tickers}
        mask = self.stocks["symbol"].str.upper().isin(wanted) | self.stocks[
            "ticker"
        ].str.upper().isin(wanted)
        return self.stocks.loc[mask].reset_index(drop=True)

    def fetch_indices(self, symbols: Sequence[str]) -> pd.DataFrame:
        self.calls.append("indices")
        return self.indices[self.indices["symbol"].isin(symbols)].reset_index(drop=True)


@pytest.fixture(scope="session")
def calendar() -> MarketCalendar:
    return MarketCalendar()


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture(scope="session")
def universe_raw() -> pd.DataFrame:
    return parse_stocks(load_payload("universe.json"))


@pytest.fixture(scope="session")
def indices_raw() -> pd.DataFrame:
    return parse_indices(load_payload("indices.json"))


@pytest.fixture(scope="session")
def samples_raw() -> pd.DataFrame:
    return parse_stocks(load_payload("discover_stocks.json"))


def stock_row(**overrides: Any) -> dict[str, Any]:
    """A complete, valid stock row with neutral values; override what a test needs."""

    row: dict[str, Any] = dict.fromkeys(STOCK_COLUMNS)
    row.update(
        symbol="NYSE:TEST",
        ticker="TEST",
        description="Test Corp",
        close=100.0,
        change_pct=0.5,
        volume=2e6,
        avg_volume_10d=2e6,
        rel_volume_10d=1.0,
        market_cap=10e9,
        sector="Finance",
        industry="Banks",
        exchange="NYSE",
        type="stock",
        subtype="common",
        country="United States",
        rec_all=0.2,
        rec_ma=0.2,
        rec_other=0.2,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        adx=25.0,
        sma20=99.0,
        sma50=95.0,
        sma200=90.0,
        ema20=99.0,
        perf_w=1.0,
        perf_1m=2.0,
        perf_3m=5.0,
        perf_ytd=10.0,
        high_52w=120.0,
        low_52w=80.0,
        premarket_change_pct=0.0,
        premarket_close=100.0,
        prev_close=99.5,
        bar_time=epoch(date(2026, 10, 30)),
        week_bar_time=epoch(date(2026, 10, 26)),
        week_close=100.0,
        prev_week_close=98.0,
        update_mode="delayed_streaming_900",
        current_session="market",
        reference_close=100.0,
    )
    row.update(overrides)
    return row
