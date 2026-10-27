"""CLI flow: gating, idempotency, dry run, sending, failure email + exit code."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeProvider, retime
from tvreport import cli
from tvreport.history import History, RunRecord
from tvreport.providers import ProviderError
from tvreport.schedule import NY

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "me@example.com",
    "SMTP_PASSWORD": "s3cret-app-password",
    "REPORT_TO": "you@example.com",
}


@pytest.fixture
def harness(monkeypatch, tmp_path, universe_raw, indices_raw):
    """Freeze time at Mon 2 Nov 2026 08:00 ET, fake provider and SMTP."""

    state: dict[str, Any] = {
        "now": datetime(2026, 11, 2, 8, 0, tzinfo=NY),
        "sent": [],
        "provider_calls": 0,
    }
    universe = retime(universe_raw, date(2026, 10, 30), date(2026, 10, 26))
    indices = retime(indices_raw, date(2026, 10, 30), date(2026, 10, 26))

    def make_provider(_cfg):
        state["provider_calls"] += 1
        return state.get("provider") or FakeProvider(universe, indices)

    monkeypatch.setattr(cli, "_now", lambda: state["now"])
    monkeypatch.setattr(cli, "make_provider", make_provider)
    monkeypatch.setattr(cli, "send", lambda msg, settings: state["sent"].append(msg))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    db, reports = tmp_path / "h.db", tmp_path / "reports"

    def run(*extra: str) -> int:
        return cli.main(
            [
                "run",
                "--db",
                str(db),
                "--reports-dir",
                str(reports),
                "--holdings",
                str(tmp_path / "none.yaml"),
                *extra,
            ]
        )

    def test_email(*extra: str) -> int:
        return cli.main(
            [
                "test-email",
                "--db",
                str(db),
                "--reports-dir",
                str(reports),
                "--holdings",
                str(tmp_path / "none.yaml"),
                *extra,
            ]
        )

    state.update(run=run, test_email=test_email, db=db, reports=reports)
    return state


def test_not_a_run_day(harness):
    harness["now"] = datetime(2026, 11, 4, 8, 0, tzinfo=NY)  # Wednesday
    assert harness["run"]("--type", "auto") == 0
    assert harness["provider_calls"] == 0


def test_outside_window_skips_without_fetching(harness):
    harness["now"] = datetime(2026, 11, 2, 10, 0, tzinfo=NY)
    assert harness["run"]("--type", "auto") == 0
    assert harness["provider_calls"] == 0 and harness["sent"] == []


def test_dry_run_saves_report_but_no_email_or_history(harness, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key)  # a dry run must not need SMTP settings
    assert harness["run"]("--type", "auto", "--dry-run") == 0
    assert (harness["reports"] / "2026-11-02-monday.html").exists()
    assert (harness["reports"] / "2026-11-02-monday.md").exists()
    assert harness["sent"] == []
    assert not harness["db"].exists()  # not even an empty history file


def test_real_run_sends_records_and_is_idempotent(harness):
    assert harness["run"]("--type", "auto") == 0
    assert len(harness["sent"]) == 1
    assert harness["sent"][0]["Subject"] == "Week Ahead — Mon 2 Nov 2026"
    assert History(harness["db"]).run_exists(date(2026, 11, 2), "monday")

    harness["now"] = datetime(2026, 11, 2, 8, 15, tzinfo=NY)  # next cron trigger
    assert harness["run"]("--type", "auto") == 0
    assert len(harness["sent"]) == 1  # not sent twice
    assert harness["provider_calls"] == 1  # second trigger never fetched data


def test_force_ignores_window_but_not_idempotency(harness):
    harness["now"] = datetime(2026, 11, 2, 12, 0, tzinfo=NY)
    assert harness["run"]("--type", "monday", "--force") == 0
    assert len(harness["sent"]) == 1
    assert harness["run"]("--type", "monday", "--force") == 0
    assert len(harness["sent"]) == 1


def test_failure_sends_failure_email_and_exits_1(harness):
    harness["provider"] = FakeProvider(None, None, error=ProviderError("HTTP 403 (not retried)"))  # type: ignore[arg-type]
    assert harness["run"]("--type", "auto") == 1
    assert len(harness["sent"]) == 1
    failure = harness["sent"][0]
    assert failure["Subject"] == "tvreport FAILED — monday 2026-11-02"
    assert "HTTP 403" in failure.get_content() and "s3cret" not in failure.get_content()
    assert not History(harness["db"]).run_exists(date(2026, 11, 2), "monday")  # retried later


def test_missing_smtp_settings_fails_before_fetching(harness, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD")
    assert harness["run"]("--type", "auto") == 1
    assert harness["provider_calls"] == 0


def test_failure_email_at_most_once_per_window(harness):
    harness["provider"] = FakeProvider(None, None, error=ProviderError("HTTP 503"))  # type: ignore[arg-type]
    assert harness["run"]("--type", "auto") == 1
    harness["now"] = datetime(2026, 11, 2, 8, 15, tzinfo=NY)  # next trigger, same window
    assert harness["run"]("--type", "auto") == 1  # still fails loudly ...
    harness["now"] = datetime(2026, 11, 2, 8, 30, tzinfo=NY)
    assert harness["run"]("--type", "auto") == 1
    assert len(harness["sent"]) == 1  # ... but only one email
    assert History(harness["db"]).alert_already_sent(date(2026, 11, 2), "monday")

    harness["now"] = datetime(2026, 11, 6, 16, 45, tzinfo=NY)  # Friday window: new alert
    assert harness["run"]("--type", "auto") == 1
    assert len(harness["sent"]) == 2
    harness["now"] = datetime(2026, 11, 9, 8, 0, tzinfo=NY)  # next Monday: new window
    assert harness["run"]("--type", "auto") == 1
    assert len(harness["sent"]) == 3


def test_failed_alert_send_is_not_recorded(harness, monkeypatch):
    def broken_send(msg, settings):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(cli, "send", broken_send)
    harness["provider"] = FakeProvider(None, None, error=ProviderError("HTTP 503"))  # type: ignore[arg-type]
    assert harness["run"]("--type", "auto") == 1
    assert not History(harness["db"]).alert_already_sent(date(2026, 11, 2), "monday")


# ------------------------------------------------------------------ test-email
def _seed_history(db: Path) -> bytes:
    """A history.db holding one recorded run; returns its exact bytes."""

    History(db).record_run(
        RunRecord(date(2026, 10, 26), "monday", "x", date(2026, 10, 23), "SP:SPX", 1.0, 1000), []
    )
    return db.read_bytes()


def test_test_email_sends_marked_report_and_writes_no_history(harness):
    before = _seed_history(harness["db"])
    harness["now"] = datetime(2026, 11, 2, 14, 0, tzinfo=NY)  # outside any run window
    assert harness["test_email"]() == 0
    assert len(harness["sent"]) == 1
    msg = harness["sent"][0]
    assert msg["Subject"].startswith("[TEST] Week Ahead — ")
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "TEST EMAIL — sent only to check delivery" in body
    assert harness["db"].read_bytes() == before  # byte-for-byte untouched
    history = History(harness["db"])
    assert not history.run_exists(date(2026, 11, 2), "monday")
    assert not history.alert_already_sent(date(2026, 11, 2), "monday")


def test_test_email_creates_no_history_file(harness):
    assert harness["test_email"]() == 0
    assert len(harness["sent"]) == 1
    assert not harness["db"].exists()


def test_test_email_failure_records_no_alert_and_sends_nothing(harness):
    before = _seed_history(harness["db"])
    harness["provider"] = FakeProvider(None, None, error=ProviderError("HTTP 503"))  # type: ignore[arg-type]
    assert harness["test_email"]() == 1
    assert harness["sent"] == []  # no failure email for a delivery test
    assert harness["db"].read_bytes() == before


def test_test_email_missing_secrets_named(harness, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_PASSWORD")
    monkeypatch.setenv("REPORT_TO", "")
    assert harness["test_email"]() == 1
    assert harness["provider_calls"] == 0 and harness["sent"] == []
    message = caplog.text
    assert "SMTP_PASSWORD" in message and "REPORT_TO" in message
    assert "SMTP_HOST" not in message.split("missing SMTP settings:")[1].split(".")[0]
    assert "s3cret" not in message


def test_dry_run_cannot_modify_an_existing_history_db(harness):
    # An older database without the `alerts` table: opening it normally would add it.
    with closing(sqlite3.connect(harness["db"])) as conn, conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, run_date TEXT, run_type TEXT)")
    before = harness["db"].read_bytes()
    assert harness["run"]("--type", "auto", "--dry-run") == 0
    assert harness["db"].read_bytes() == before
