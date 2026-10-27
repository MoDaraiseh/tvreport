"""Cheap pre-check for scheduled triggers (standard library only).

The workflow fires about 28 times a week (every 15 minutes across the EDT and
EST equivalents of each window). This script lets most of those exit in seconds,
before any dependency is installed, when the trigger is clearly outside a run
window or the report for today is already recorded in data/history.db.

It is deliberately a *superset* of the real gate in tvreport.schedule (whose
Friday lower bound depends on the NYSE calendar's early closes): anything it
lets through is re-checked precisely by `python -m tvreport run --type auto`.

Writes `run=true|false` to $GITHUB_OUTPUT (or stdout).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
# Broadest possible windows: Monday 06:30-09:25 ET; Friday from 13:30 (close +30 min
# on an early-close day) to 18:15 ET.
WINDOWS = {0: ("monday", time(6, 30), time(9, 25)), 4: ("friday", time(13, 30), time(18, 15))}
DB = Path(__file__).resolve().parents[1] / "data" / "history.db"


def decide(now: datetime, db: Path = DB) -> tuple[bool, str]:
    local = now.astimezone(NY)
    window = WINDOWS.get(local.weekday())
    if window is None:
        return False, f"{local:%a %H:%M %Z}: not a run day"
    run_type, start, end = window
    if not start <= local.time() <= end:
        return False, f"{local:%a %H:%M %Z}: outside the broad {run_type} window"
    if db.exists():
        # closing(): sqlite3's own context manager commits but does not close
        with closing(sqlite3.connect(db)) as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM runs WHERE run_date = ? AND run_type = ?",
                    (local.date().isoformat(), run_type),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # no runs table yet
        if row is not None:
            return False, f"{local:%a %Y-%m-%d}: {run_type} report already recorded"
    return True, f"{local:%a %H:%M %Z}: candidate {run_type} run"


def main() -> int:
    run, reason = decide(datetime.now(UTC))
    print(reason)
    line = f"run={'true' if run else 'false'}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(line)
    else:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
