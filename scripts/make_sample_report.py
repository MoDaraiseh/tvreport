"""Build docs/sample-report.html: a Monday ("Week Ahead") report from the
recorded test fixtures, not live data, clearly labelled as a sample.

The fixtures (tests/fixtures/*.json) are TradingView scanner responses recorded
during the 27 Oct 2026 session with scripts/discover_fields.py. As in the tests,
they are re-dated to the Fri 30 Oct session and the report is built as if run at
08:00 ET on Mon 2 Nov 2026. Nothing is fetched, emailed or recorded.

Usage (from the repository root):  python -m scripts.make_sample_report
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from tests.conftest import FakeProvider, load_payload, retime

from tvreport.config import load_config
from tvreport.history import History
from tvreport.pipeline import build_report
from tvreport.providers.tradingview import parse_indices, parse_stocks
from tvreport.report import render_html
from tvreport.schedule import NY, MarketCalendar

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "sample-report.html"
RUN_AT = datetime(2026, 11, 2, 8, 0, tzinfo=NY)
SESSION = date(2026, 10, 30)
WEEK_START = date(2026, 10, 26)
BANNER = (
    '<p style="font-size:13px;color:#1e3a8a;background:#dbeafe;padding:6px 8px;'
    'border-radius:4px;">SAMPLE REPORT: built from the recorded test fixtures '
    "(TradingView data recorded during the 27 Oct 2026 session, re-dated to the "
    "30 Oct session), not from a scheduled run. Prices and picks are not current. "
    "Not financial advice.</p>\n"
)


def build_sample_html() -> str:
    universe = retime(parse_stocks(load_payload("universe.json")), SESSION, WEEK_START)
    indices = retime(parse_indices(load_payload("indices.json")), SESSION, WEEK_START)
    with tempfile.TemporaryDirectory() as tmp:
        history = History(Path(tmp) / "history.db")
        outcome = build_report(
            "monday",
            RUN_AT,
            load_config(),
            FakeProvider(universe, indices),
            history,
            dry_run=True,
            holdings_path=Path(tmp) / "no-holdings.yaml",
            calendar=MarketCalendar(),
        )
    # Built as a dry run (nothing recorded), rendered without the DRY RUN label:
    # the SAMPLE banner below says what it is.
    html = render_html(replace(outcome.report, dry_run=False))
    html = html.replace("<title>", "<title>[SAMPLE] ", 1)
    head, sep, tail = html.partition("<h1 ")
    if not sep:
        raise RuntimeError("report template has no <h1>; update make_sample_report.py")
    return head + BANNER + sep + tail


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_sample_html(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
