"""Configuration loading and validation (config.yaml -> pydantic models).

Everything tunable lives in config.yaml; this module validates it at startup so
a bad value fails immediately with a clear message instead of producing a
quietly wrong report.
"""

from __future__ import annotations

import math
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseConfig(_Strict):
    exchanges: list[str] = ["NYSE", "NASDAQ", "AMEX"]
    min_market_cap: float = Field(2e9, gt=0)
    min_avg_volume_10d: float = Field(1e6, ge=0)
    min_price: float = Field(5.0, gt=0)
    min_size: int = Field(200, ge=1)
    max_size: int = Field(3000, ge=1)
    include_foreign_domiciled: bool = True
    """US-listed ordinary shares of companies domiciled abroad (Linde, Accenture,
    Medtronic, ...) are common stock, not ADRs. False keeps country == United States only."""

    @model_validator(mode="after")
    def _size_bounds(self) -> UniverseConfig:
        if self.min_size >= self.max_size:
            raise ValueError("universe.min_size must be smaller than universe.max_size")
        return self


class IndexConfig(_Strict):
    name: str
    symbol: str
    fallback_symbol: str | None = None
    """Used (and labeled as such) only when `symbol` does not yet reflect the
    expected session, e.g. an official index whose close publishes late."""


class Weights(_Strict):
    tv_rating: float = Field(0.30, ge=0)
    trend: float = Field(0.25, ge=0)
    momentum: float = Field(0.25, ge=0)
    volume: float = Field(0.10, ge=0)
    range_52w: float = Field(0.10, ge=0)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Weights:
        total = self.tv_rating + self.trend + self.momentum + self.volume + self.range_52w
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"weights must sum to 1.0, got {total:.6f}")
        return self


class ClassifyConfig(_Strict):
    n_buy: int = Field(10, ge=0)
    n_sell: int = Field(10, ge=0)
    n_hold: int = Field(10, ge=0)
    max_per_sector: int = Field(3, ge=1)
    buy_min_recommend: float = Field(0.1, ge=-1, le=1)
    buy_max_rsi: float = Field(75, ge=0, le=100)
    sell_max_recommend: float = Field(-0.1, ge=-1, le=1)
    extended_rsi: float = Field(70, ge=0, le=100)
    extended_pct_above_sma50: float = Field(15.0, ge=0)
    hold_min_score: float = Field(60, ge=0, le=100)


class PortfolioConfig(_Strict):
    buy_more_min_score: float = Field(65, ge=0, le=100)
    sell_below_score: float = Field(40, ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> PortfolioConfig:
        if self.sell_below_score > self.buy_more_min_score:
            raise ValueError("portfolio.sell_below_score must be <= portfolio.buy_more_min_score")
        return self


def _parse_hhmm(value: object) -> time:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected 'HH:MM', got {value!r}")
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm))


class MondayWindow(_Strict):
    target: time = time(8, 0)
    earliest: time = time(6, 30)
    latest: time = time(9, 25)

    parse_times = field_validator("target", "earliest", "latest", mode="before")(_parse_hhmm)

    @model_validator(mode="after")
    def _ordered(self) -> MondayWindow:
        if not self.earliest <= self.target <= self.latest:
            raise ValueError("schedule.monday must satisfy earliest <= target <= latest")
        if self.latest >= time(9, 30):
            raise ValueError("schedule.monday.latest must be before the 09:30 ET open")
        return self


class FridayWindow(_Strict):
    target: time = time(16, 45)
    minutes_after_close: int = Field(30, ge=15)
    """Never run before the session close plus this many minutes. Data is 15-minute
    delayed, so anything earlier could capture an intraday price."""
    latest: time = time(18, 15)

    parse_times = field_validator("target", "latest", mode="before")(_parse_hhmm)


class ScheduleConfig(_Strict):
    timezone: Literal["America/New_York"] = "America/New_York"
    monday: MondayWindow = MondayWindow()
    friday: FridayWindow = FridayWindow()


class ProviderConfig(_Strict):
    name: Literal["tradingview"] = "tradingview"
    timeout_seconds: float = Field(30, gt=0)
    max_attempts: int = Field(3, ge=1, le=5)
    min_interval_seconds: float = Field(1.0, ge=0)
    backoff_base_seconds: float = Field(2.0, ge=0)


class DataQualityConfig(_Strict):
    max_drop_fraction: float = Field(0.10, ge=0, le=1)


class ReportConfig(_Strict):
    top_movers: int = Field(10, ge=1)
    display_timezones: list[str] = ["America/New_York", "Asia/Dubai"]


class Config(_Strict):
    universe: UniverseConfig = UniverseConfig()
    indices: list[IndexConfig]
    weights: Weights = Weights()
    classify: ClassifyConfig = ClassifyConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    provider: ProviderConfig = ProviderConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    report: ReportConfig = ReportConfig()
    benchmark_symbol: str = "SP:SPX"

    @model_validator(mode="after")
    def _benchmark_is_an_index(self) -> Config:
        if self.benchmark_symbol not in {i.symbol for i in self.indices}:
            raise ValueError("benchmark_symbol must be one of the configured indices")
        return self


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
