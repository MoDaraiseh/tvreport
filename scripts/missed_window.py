"""A trigger after today's catch-up cutoff, with no report sent today. (stdlib only)

The first such trigger per window notes the miss in data/history.db, sends the
shared once-per-window failure email (scripts/notify_failure.py) and exits 1, so
the run fails. Every later late trigger in the same window finds the note and
exits 0 quietly, so a missed window shows one failed run, not one per trigger.
The note is written before the email is tried, so it holds even when SMTP is not
configured. The workflow commits history.db afterwards so later triggers see it.

Usage (from the repository root): python3 -m scripts.missed_window <monday|friday> "<run url>"
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import notify_failure

NY = ZoneInfo("America/New_York")
DB = Path(__file__).resolve().parents[1] / "data" / "history.db"
# Same DDL as tvreport.history.MISSED_DDL
MISSED_DDL = """CREATE TABLE IF NOT EXISTS missed_windows (
    run_date     TEXT NOT NULL,
    run_type     TEXT NOT NULL,
    noted_at_utc TEXT NOT NULL,
    PRIMARY KEY (run_date, run_type)
);
"""


def already_noted(db: Path, run_date: date, run_type: str) -> bool:
    if not db.exists():
        return False
    with closing(sqlite3.connect(db)) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM missed_windows WHERE run_date = ? AND run_type = ?",
                (run_date.isoformat(), run_type),
            ).fetchone()
        except sqlite3.OperationalError:  # no missed_windows table yet
            return False
    return row is not None


def note(db: Path, run_date: date, run_type: str, now: datetime) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(MISSED_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO missed_windows VALUES (?, ?, ?)",
            (run_date.isoformat(), run_type, now.isoformat(timespec="seconds")),
        )


def main(argv: list[str], db: Path = DB, now: datetime | None = None) -> int:
    run_type = argv[1] if len(argv) > 1 else ""
    url = argv[2] if len(argv) > 2 else ""
    if run_type not in ("monday", "friday"):
        print(
            f"usage: missed_window.py monday|friday <run url> (got {run_type!r})", file=sys.stderr
        )
        return 2
    now = now or datetime.now(UTC)
    run_date = now.astimezone(NY).date()
    if already_noted(db, run_date, run_type):
        print(f"the missed {run_type} window of {run_date} was already reported; ending quietly")
        return 0
    note(db, run_date, run_type, now)
    notify_failure.main(
        [
            "notify_failure.py",
            run_type,
            "missed window: the trigger arrived after the catch-up cutoff",
            url,
        ],
        db=db,
    )
    print(f"::error::Today's {run_type} report was not sent and its catch-up period has ended.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
