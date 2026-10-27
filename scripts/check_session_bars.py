"""Show which session TradingView's daily and weekly bars belong to right now.

Run this during a pre-market window (especially a Monday before 09:30 ET) to see
which case tvreport will hit: whether `time` (daily bar) and `time|1W` (weekly
bar) still point to the previous session/week or have already rolled over, and
what `close`, `close[1]`, `change` and relative volume look like at that moment.

Usage:  python scripts/check_session_bars.py
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from tvreport.config import load_config
from tvreport.providers.tradingview import TradingViewProvider
from tvreport.quality import bar_dates
from tvreport.schedule import NY

SAMPLES = ["NASDAQ:AAPL", "NYSE:JPM", "NASDAQ:NVDA"]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    log = logging.getLogger("check_session_bars")
    cfg = load_config()
    provider = TradingViewProvider(cfg.provider)
    log.info("now: %s", datetime.now(UTC).astimezone(NY).strftime("%a %Y-%m-%d %H:%M %Z"))

    stocks = provider.fetch_stocks(SAMPLES)
    stocks = stocks.assign(
        bar=bar_dates(stocks["bar_time"]), week=bar_dates(stocks["week_bar_time"])
    )
    log.info(
        "%s",
        stocks[
            [
                "symbol",
                "bar",
                "week",
                "current_session",
                "close",
                "prev_close",
                "change_pct",
                "rel_volume_10d",
                "premarket_close",
            ]
        ].to_string(index=False),
    )

    indices = provider.fetch_indices([i.symbol for i in cfg.indices])
    indices = indices.assign(
        bar=bar_dates(indices["bar_time"]), week=bar_dates(indices["week_bar_time"])
    )
    log.info(
        "%s",
        indices[
            ["symbol", "bar", "week", "close", "prev_close", "week_close", "prev_week_close"]
        ].to_string(index=False),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
