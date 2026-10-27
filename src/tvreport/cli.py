"""Command line: python -m tvreport run --type monday|friday|auto [--dry-run] [--force]

Order of operations for a real (non-dry) run:
  1. resolve the run type ('auto' = by New York weekday) and check the time
     window (skipped with --force, e.g. a manual workflow_dispatch run);
  2. skip quietly if this date + run type is already in history.db (idempotency);
  3. build the report, save reports/YYYY-MM-DD-<type>.html/.md, email it;
  4. record the run in history.db only after the email was sent.
Any failure exits 1 and sends a short failure email (no secrets) -- at most one per
run type per scheduled window; later failures in the same window only log.
A dry run builds and saves the report but neither emails nor writes history.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import traceback
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from .config import PROJECT_ROOT, load_config
from .history import DEFAULT_DB_PATH, History
from .notify import (
    NotifyConfigError,
    SmtpSettings,
    build_message,
    failure_message,
    scrub,
    send,
    smtp_settings,
)
from .pipeline import build_report, record_outcome
from .providers import make_provider
from .report import render_html, render_text, save_report
from .schedule import NY, MarketCalendar, gate, resolve_run_type

log = logging.getLogger("tvreport")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tvreport",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build (and normally email) a report")
    run.add_argument("--type", choices=["monday", "friday", "auto"], required=True)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build and save the report; don't email, don't write history",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="ignore the time window (manual runs); idempotency still applies",
    )
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--holdings", type=Path, default=None)
    run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    run.add_argument("--reports-dir", type=Path, default=PROJECT_ROOT / "reports")

    test = sub.add_parser(
        "test-email",
        help="email the current dry-run report with [TEST] in the subject, to verify SMTP "
        "delivery; never writes history, records a run or an alert",
    )
    test.add_argument(
        "--type",
        choices=["monday", "friday"],
        default="monday",
        help="report to build (friday needs a closed session); default monday",
    )
    test.add_argument("--config", type=Path, default=None)
    test.add_argument("--holdings", type=Path, default=None)
    test.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    test.add_argument("--reports-dir", type=Path, default=PROJECT_ROOT / "reports")
    return p


def _now() -> datetime:
    return datetime.now(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args(argv)
    if args.command == "test-email":
        return _test_email(args)
    now = _now()

    run_type = resolve_run_type(args.type, now)
    if run_type is None:
        log.info(
            "%s is neither Monday nor Friday in New York; nothing to do",
            now.astimezone(NY).strftime("%a %Y-%m-%d %H:%M %Z"),
        )
        return 0

    cfg = load_config(args.config)
    calendar = MarketCalendar()
    if not args.force:
        decision = gate(run_type, now, cfg.schedule, calendar)
        log.info("gate: %s", decision.reason)
        if not decision.proceed:
            return 0

    # A dry run never writes history (not even a schema update or an empty file).
    history = _scratch_history(args.db) if args.dry_run else History(args.db)
    run_date = now.astimezone(NY).date()
    if not args.dry_run and history.run_exists(run_date, run_type):
        log.info(
            "a %s report for %s is already recorded; skipping (idempotency)", run_type, run_date
        )
        return 0

    settings: SmtpSettings | None = None
    label = f"{run_type} {run_date.isoformat()}"
    try:
        if not args.dry_run:
            settings = smtp_settings()  # fail before fetching anything if SMTP isn't configured
        provider = make_provider(cfg.provider)
        outcome = build_report(
            run_type,
            now,
            cfg,
            provider,
            history,
            dry_run=args.dry_run,
            holdings_path=args.holdings,
            calendar=calendar,
        )
        html_path, md_path = save_report(outcome.report, args.reports_dir)
        log.info("saved %s and %s", html_path, md_path)
        if args.dry_run:
            log.info("dry run: not emailing, not writing history")
            return 0
        assert settings is not None
        report = outcome.report
        send(
            build_message(settings, report.subject, render_text(report), render_html(report)),
            settings,
        )
        record_outcome(history, outcome)
        log.info("recorded %s in %s", label, args.db)
        return 0
    except Exception as exc:
        log.error("run failed: %s", scrub(f"{type(exc).__name__}: {exc}"))
        log.error("traceback:\n%s", scrub("".join(traceback.format_exception(exc))))
        if settings is not None:
            _alert_once(
                history,
                settings=settings,
                run_date=run_date,
                run_type=run_type,
                label=label,
                exc=exc,
            )
        return 1  # always non-zero, even when the alert was already sent


def _alert_once(
    history: History,
    *,
    settings: SmtpSettings,
    run_date: date,
    run_type: str,
    label: str,
    exc: BaseException,
) -> None:
    """At most one failure email per run type per scheduled window (= New York date):
    later failing triggers in the same window only log. The alert is recorded only
    after it was actually sent, so a failed send is retried by the next trigger."""

    try:
        if history.alert_already_sent(run_date, run_type):
            log.warning(
                "a failure alert for %s was already sent in this window; not emailing again", label
            )
            return
        send(failure_message(settings, label, exc), settings)
        history.record_alert(run_date, run_type, _now().isoformat(timespec="seconds"))
    except Exception as mail_exc:
        log.error("could not send the failure email: %s", scrub(str(mail_exc), settings=settings))


def _scratch_history(db: Path) -> History:
    """History backed by a temporary copy of `db`: reads see the real data (e.g.
    Monday's picks for a Friday report) but no write can ever reach `db`."""

    scratch = Path(tempfile.mkdtemp(prefix="tvreport-scratch-")) / "history.db"
    if db.exists():
        shutil.copy2(db, scratch)
    return History(scratch)


def _test_email(args: argparse.Namespace) -> int:
    """Build the current dry-run report and email it with [TEST] in the subject.

    Never touches data/history.db: no run, picks or failure alert is recorded, so
    it can't make the real Monday/Friday run skip or suppress anything."""

    try:
        settings = smtp_settings()
    except NotifyConfigError as exc:
        log.error(
            "cannot send the test email -- %s. Add them as repository secrets (Settings > "
            "Secrets and variables > Actions) or, locally, as environment variables.",
            exc,
        )
        return 1
    now = _now()
    try:
        cfg = load_config(args.config)
        outcome = build_report(
            args.type,
            now,
            cfg,
            make_provider(cfg.provider),
            _scratch_history(args.db),
            dry_run=True,
            holdings_path=args.holdings,
            calendar=MarketCalendar(),
        )
        report = replace(outcome.report, test_email=True)
        save_report(report, args.reports_dir)
        send(
            build_message(settings, report.subject, render_text(report), render_html(report)),
            settings,
        )
    except Exception as exc:
        log.error("test email failed: %s", scrub(f"{type(exc).__name__}: {exc}", settings=settings))
        log.error(
            "traceback:\n%s",
            scrub("".join(traceback.format_exception(exc)), settings=settings),
        )
        return 1
    log.info(
        "test email sent to %d recipient(s); nothing was recorded in history", len(settings.to)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
