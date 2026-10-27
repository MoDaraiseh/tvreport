"""Send a short failure email using only the standard library.

Used by the workflow's `if: failure()` step for failures the Python package
can't report itself (e.g. dependency installation failed). Reads the same
SMTP_* / REPORT_TO environment variables and never prints them.

Rate limit: at most one failure email per run type per scheduled window, shared
with the CLI through the `alerts` table in data/history.db (same DDL as
tvreport.history). A window is identified by (New York date, run type). The
workflow commits history.db afterwards so later triggers see the record.

Usage: python3 scripts/notify_failure.py <monday|friday> "<what failed>" "<run url>"
"""

from __future__ import annotations

import os
import smtplib
import sqlite3
import ssl
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "REPORT_TO")
DB = Path(__file__).resolve().parents[1] / "data" / "history.db"
ALERTS_DDL = """CREATE TABLE IF NOT EXISTS alerts (
    run_date    TEXT NOT NULL,
    run_type    TEXT NOT NULL,
    sent_at_utc TEXT NOT NULL,
    PRIMARY KEY (run_date, run_type)
);
"""


def alert_already_sent(db: Path, run_date: date, run_type: str) -> bool:
    if not db.exists():
        return False
    with closing(sqlite3.connect(db)) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE run_date = ? AND run_type = ?",
                (run_date.isoformat(), run_type),
            ).fetchone()
        except sqlite3.OperationalError:  # no alerts table yet
            return False
    return row is not None


def record_alert(db: Path, run_date: date, run_type: str, now: datetime) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(ALERTS_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO alerts VALUES (?, ?, ?)",
            (run_date.isoformat(), run_type, now.isoformat(timespec="seconds")),
        )


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["REPORT_TO"]
    msg.set_content(body)
    port = int(os.environ["SMTP_PORT"])
    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            os.environ["SMTP_HOST"], port, timeout=30, context=context
        )
    else:
        server = smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30)
        server.starttls(context=context)
    with server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def main(argv: list[str], db: Path = DB) -> int:
    run_type = argv[1] if len(argv) > 1 else "unknown"
    what = argv[2] if len(argv) > 2 else "workflow"
    url = argv[3] if len(argv) > 3 else ""
    now = datetime.now(UTC)
    run_date = now.astimezone(NY).date()
    if alert_already_sent(db, run_date, run_type):
        print(f"a failure alert for {run_type} {run_date} was already sent; not emailing again")
        return 0
    if any(not os.environ.get(k, "").strip() for k in KEYS):
        print("SMTP settings missing; cannot send the failure email", file=sys.stderr)
        return 1
    try:
        send_email(
            f"tvreport FAILED — {run_type} {run_date.isoformat()}",
            f"The tvreport GitHub Actions run failed ({what}); no report was sent.\n\n"
            f"Run log: {url}\n\nLater failures in this window will not email again.\n",
        )
    except (smtplib.SMTPException, OSError) as exc:
        print(f"failure email could not be sent: {type(exc).__name__}", file=sys.stderr)
        return 1
    record_alert(db, run_date, run_type, now)
    print("failure email sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
