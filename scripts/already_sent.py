"""Was today's report of this type already sent? (standard library only)

Runs in the workflow right after the time-window gate and checkout, before any
dependency is installed: once a report is recorded in data/history.db, the rest
of that window's triggers finish in seconds. The CLI re-checks this anyway.

Usage:  python3 scripts/already_sent.py monday|friday
Writes `sent=true|false` to $GITHUB_OUTPUT (or stdout).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DB = Path(__file__).resolve().parents[1] / "data" / "history.db"


def already_sent(run_type: str, run_date: date, db: Path = DB) -> bool:
    if not db.exists():
        return False
    # closing(): sqlite3's own context manager commits but does not close
    with closing(sqlite3.connect(db)) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_date = ? AND run_type = ?",
                (run_date.isoformat(), run_type),
            ).fetchone()
        except sqlite3.OperationalError:  # no runs table yet
            return False
    return row is not None


def main(argv: list[str]) -> int:
    run_type = argv[1] if len(argv) > 1 else ""
    if run_type not in ("monday", "friday"):
        print(f"usage: already_sent.py monday|friday (got {run_type!r})", file=sys.stderr)
        return 2
    run_date = datetime.now(UTC).astimezone(NY).date()
    sent = already_sent(run_type, run_date)
    print(f"{run_type} report for {run_date}: {'already sent' if sent else 'not sent yet'}")
    line = f"sent={'true' if sent else 'false'}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(line)
    else:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
