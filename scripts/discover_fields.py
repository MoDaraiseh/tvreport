"""Live field discovery against TradingView's scanner endpoint.

Queries every field tvreport uses for AAPL, MSFT, NVDA, JPM and XOM, prints
each field with its sample values, flags any field that comes back null for
every sample (the endpoint does NOT reject unknown field names -- they silently
return null), checks units/scales from the data itself, and saves the raw
responses as test fixtures:

    tests/fixtures/discover_stocks.json   the 5 sample stocks
    tests/fixtures/universe.json          the full filtered universe (one request)
    tests/fixtures/indices.json           the configured indices

Usage:  python scripts/discover_fields.py
"""

from __future__ import annotations

import json
import logging
import math
import sys
from collections.abc import Mapping
from typing import Any

from tvreport.config import PROJECT_ROOT, load_config
from tvreport.providers.tradingview import (
    INDEX_FIELDS,
    STOCK_FIELDS,
    TradingViewProvider,
    index_query,
    parse_stocks,
    ticker_query,
    universe_query,
)

log = logging.getLogger("discover_fields")
SAMPLES = ["NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA", "NYSE:JPM", "NYSE:XOM"]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _save(name: str, columns: list[str], payload: Mapping[str, Any]) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps({"columns": columns, "response": payload}, indent=1), "utf-8")
    log.info("saved %s (%d rows)", path.relative_to(PROJECT_ROOT).as_posix(), len(payload["data"]))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def check_units(rows: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Scale/unit checks derived from the data itself; returns problems found."""

    problems: list[str] = []
    for ticker, r in rows.items():
        close, prev, chg = _num(r["close"]), _num(r["close[1]"]), _num(r["change"])
        if close and prev and chg is not None:
            implied = 100 * (close / prev - 1)
            if not math.isclose(implied, chg, abs_tol=0.05):
                problems.append(
                    f"{ticker}: change={chg:.4f}, but 100*(close/close[1]-1)={implied:.4f}"
                )
        for field in ("Recommend.All", "Recommend.MA", "Recommend.Other"):
            value = _num(r[field])
            if value is None or not -1 <= value <= 1:
                problems.append(f"{ticker}: {field}={r[field]!r} is outside -1..1")
        rsi = _num(r["RSI"])
        if rsi is None or not 0 <= rsi <= 100:
            problems.append(f"{ticker}: RSI={r['RSI']!r} is outside 0..100")
        high, low = _num(r["price_52_week_high"]), _num(r["price_52_week_low"])
        if close and high and low and not low <= close <= high * 1.0001:
            problems.append(f"{ticker}: close {close} outside 52-week range {low}..{high}")
    return problems


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    cfg = load_config()
    provider = TradingViewProvider(cfg.provider)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    # 1. Five sample stocks, every field.
    stock_fields = list(STOCK_FIELDS.values())
    payload = provider.execute(ticker_query(SAMPLES), "discover")
    _save("discover_stocks.json", stock_fields, payload)
    rows = {item["s"]: dict(zip(stock_fields, item["d"], strict=True)) for item in payload["data"]}
    absent = sorted(set(SAMPLES) - set(rows))
    if absent:
        log.error("sample tickers not returned: %s", absent)
        return 1
    ordered = {t: rows[t] for t in SAMPLES}

    log.info("\n%-26s %s", "field", "".join(f"{t.split(':')[1]:>16s}" for t in SAMPLES))
    for field in stock_fields:
        values = [ordered[t][field] for t in SAMPLES]
        log.info("%-26s %s", field, "".join(f"{str(v)[:15]:>16s}" for v in values))
        if all(v is None for v in values):
            problems.append(f"{field}: null for every sample -> missing or misspelled")

    problems += check_units(ordered)
    log.info(
        "\nUnits: `change`, `Perf.*` and `premarket_change` are percentages (`change` is "
        "checked above against close/close[1]); Recommend.* is -1..1; RSI is 0..100."
    )
    log.info("Perf.W vs calendar week-over-week (close / close[1]|1W - 1), for reference:")
    for ticker, r in ordered.items():
        close, prev_week = _num(r["close"]), _num(r["close[1]|1W"])
        if close and prev_week:
            log.info(
                "   %-12s Perf.W=%8.3f%%   week-over-week=%8.3f%%",
                ticker,
                r["Perf.W"],
                100 * (close / prev_week - 1),
            )

    # 2. Full universe in one request.
    upayload = provider.execute(universe_query(cfg.universe), "universe")
    _save("universe.json", stock_fields, upayload)
    universe = parse_stocks(upayload)
    log.info("universe: totalCount=%s rows=%d", upayload.get("totalCount"), len(universe))
    if int(upayload.get("totalCount", 0)) > len(universe):
        problems.append("universe response was truncated")

    # 3. Indices.
    symbols = [i.symbol for i in cfg.indices] + [
        i.fallback_symbol for i in cfg.indices if i.fallback_symbol
    ]
    ipayload = provider.execute(index_query(symbols), "indices")
    _save("indices.json", list(INDEX_FIELDS.values()), ipayload)
    returned = {item["s"]: item["d"] for item in ipayload["data"]}
    for index in cfg.indices:
        if index.symbol in returned:
            index_values = dict(zip(INDEX_FIELDS.values(), returned[index.symbol], strict=True))
            log.info(
                "index %-17s %-12s %-9s close=%s",
                index.name,
                index.symbol,
                index_values["type"],
                index_values["close"],
            )
        else:
            problems.append(f"index {index.symbol} ({index.name}) not returned")

    log.info("\nrequests made: %d", provider.request_count)
    if problems:
        log.error("\nPROBLEMS:\n  %s", "\n  ".join(problems))
        return 1
    log.info("\nAll fields present; units as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
