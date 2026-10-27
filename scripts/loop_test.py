"""End-to-end loop test against live data: Monday run -> history -> Friday report.

Uses a scratch database (never data/history.db) and sends no email:

  1. builds a Monday-style ("Week Ahead") report from live data and records it,
     with its picks and reference prices, in the scratch database;
  2. builds a Friday-style ("Week in Review") report from live data against that
     database, so Monday's picks are evaluated against the S&P 500, and records
     that run too (weekly changes, pick results).

Both reports are saved to --out. Run it after the session close + 30 minutes
(the Friday report requires today's completed session). On a non-Monday day the
"Monday" run's reference prices are the previous session's closes, so the
evaluated window is previous close -> today's close.

Usage:  python scripts/loop_test.py --db <scratch.db> --out <dir>
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from tvreport.config import load_config
from tvreport.history import DEFAULT_DB_PATH, History
from tvreport.pipeline import build_report, record_outcome
from tvreport.providers import make_provider
from tvreport.report import save_report
from tvreport.schedule import NY, MarketCalendar


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, required=True, help="scratch SQLite path")
    parser.add_argument("--out", type=Path, required=True, help="directory for the reports")
    args = parser.parse_args(argv)
    if args.db.resolve() == DEFAULT_DB_PATH.resolve():
        parser.error("refusing to use data/history.db; pass a scratch database path")

    cfg = load_config()
    calendar = MarketCalendar()
    provider = make_provider(cfg.provider)
    history = History(args.db)
    now = datetime.now(UTC)
    print(f"now: {now.astimezone(NY):%a %Y-%m-%d %H:%M %Z}; scratch db: {args.db}")

    monday = build_report(
        "monday",
        now,
        cfg,
        provider,
        history,
        dry_run=False,
        holdings_path=args.out / "no-holdings.yaml",
        calendar=calendar,
    )
    record_outcome(history, monday)
    paths = save_report(monday.report, args.out / "monday")
    print(
        f"recorded Monday-style run: {len(monday.report.buy)} BUY / "
        f"{len(monday.report.sell)} SELL picks; benchmark ref "
        f"{monday.run_record.benchmark_ref}; saved {paths[1]}"
    )

    friday = build_report(
        "friday",
        now,
        cfg,
        provider,
        history,
        dry_run=False,
        holdings_path=args.out / "no-holdings.yaml",
        calendar=calendar,
    )
    record_outcome(history, friday)
    paths = save_report(friday.report, args.out / "friday")
    perf = friday.report.performance
    if perf is None or not perf.rows:
        print("FAIL: the Friday report has no Monday-picks performance rows", file=sys.stderr)
        return 1
    print(
        f"Friday-style report evaluated {len(perf.rows)} picks; S&P 500 over the window: "
        f"{perf.benchmark_return_pct}; saved {paths[1]}"
    )
    print(f"scratch db now holds {len(history.all_results())} pick results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
