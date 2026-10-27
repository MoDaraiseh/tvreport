"""Portfolio mode: every holding in holdings.yaml gets BUY MORE / HOLD / SELL.

Action from the composite score: >= buy_more_min_score -> BUY MORE;
< sell_below_score -> SELL; otherwise HOLD. Holdings outside the universe are
fetched separately and scored against the universe's percentile distribution.
A holding the provider can't return, or that lacks a field the score needs, is
listed as "N/A" with the reason -- never silently dropped.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from .config import PROJECT_ROOT, PortfolioConfig, Weights
from .providers import DataProvider
from .quality import REQUIRED_STOCK_FIELDS
from .scoring import rationale, score_frame

log = logging.getLogger(__name__)
DEFAULT_HOLDINGS_PATH = PROJECT_ROOT / "holdings.yaml"


class Holding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str = Field(min_length=1)
    shares: float = Field(gt=0)
    avg_cost: float | None = Field(default=None, gt=0)


class Holdings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    holdings: list[Holding]


def load_holdings(path: Path | None = None) -> list[Holding] | None:
    """Holdings from holdings.yaml, or None if the file does not exist."""

    path = path or DEFAULT_HOLDINGS_PATH
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(Holdings.model_validate(data).holdings)


def _key(ticker: str) -> str:
    return ticker.strip().upper()


Lookup = tuple[dict[str, pd.Series], dict[str, pd.Series]]


def _lookup(frame: pd.DataFrame) -> Lookup:
    """Rows keyed by exchange-qualified symbol and by bare ticker (first wins)."""

    by_symbol: dict[str, pd.Series] = {}
    by_ticker: dict[str, pd.Series] = {}
    for _, row in frame.iterrows():
        by_symbol[str(row["symbol"]).upper()] = row
        by_ticker.setdefault(str(row["ticker"]).upper(), row)
    return by_symbol, by_ticker


def _find(lookup: Lookup, key: str) -> pd.Series | None:
    by_symbol, by_ticker = lookup
    return by_symbol.get(key) if ":" in key else by_ticker.get(key)


def evaluate_portfolio(
    holdings: list[Holding],
    universe_scored: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    provider: DataProvider,
    weights: Weights,
    cfg: PortfolioConfig,
    premarket_bar: bool,
    volume_available: bool,
) -> pd.DataFrame:
    in_universe = _lookup(universe_scored)
    outside = [h.ticker for h in holdings if _find(in_universe, _key(h.ticker)) is None]
    extra_scored = pd.DataFrame()
    if outside:
        extra = provider.fetch_stocks(outside)
        if not extra.empty:
            extra["reference_close"] = extra["prev_close"] if premarket_bar else extra["close"]
            complete = extra.dropna(subset=list(REQUIRED_STOCK_FIELDS))
            incomplete = extra.drop(complete.index)
            parts = []
            if not complete.empty:
                parts.append(
                    score_frame(
                        complete, weights, reference=universe, volume_available=volume_available
                    )
                )
            if not incomplete.empty:
                parts.append(incomplete.assign(score=float("nan")))
            extra_scored = pd.concat(parts, ignore_index=True)
    outside_found = _lookup(extra_scored)

    rows = []
    for h in holdings:
        key = _key(h.ticker)
        row = _find(in_universe, key)
        found_in_universe = row is not None
        if row is None:
            row = _find(outside_found, key)
        base = {
            "ticker": key,
            "symbol": None,
            "description": "",
            "sector": "",
            "shares": h.shares,
            "avg_cost": h.avg_cost,
            "price": None,
            "score": None,
            "action": "N/A",
            "note": "not returned by the data provider",
            "pnl": None,
            "pnl_pct": None,
            "in_universe": found_in_universe,
        }
        if row is None:
            rows.append(base)
            continue
        price = float(row["reference_close"]) if pd.notna(row.get("reference_close")) else None
        raw_score = row.get("score")
        score = None if raw_score is None or pd.isna(raw_score) else float(raw_score)
        if score is None:
            action, note = "N/A", "missing data needed for the score"
        elif score >= cfg.buy_more_min_score:
            action, note = "BUY MORE", rationale(row, include_volume=volume_available)
        elif score < cfg.sell_below_score:
            action, note = "SELL", rationale(row, include_volume=volume_available)
        else:
            action, note = "HOLD", rationale(row, include_volume=volume_available)
        pnl = pnl_pct = None
        if h.avg_cost is not None and price is not None:
            pnl = (price - h.avg_cost) * h.shares
            pnl_pct = 100.0 * (price / h.avg_cost - 1.0)
        base.update(
            ticker=str(row["ticker"]),
            symbol=str(row["symbol"]),
            description=str(row.get("description") or ""),
            sector=str(row.get("sector") or ""),
            price=price,
            score=score,
            action=action,
            note=note,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )
        rows.append(base)
    return pd.DataFrame(rows)
