from __future__ import annotations

from ..config import ProviderConfig
from .base import DataProvider, ProviderError
from .tradingview import TradingViewProvider

__all__ = ["DataProvider", "ProviderError", "make_provider"]


def make_provider(cfg: ProviderConfig) -> DataProvider:
    if cfg.name == "tradingview":
        return TradingViewProvider(cfg)
    raise ValueError(f"unknown provider {cfg.name!r}")
