"""CLI flow: gating, idempotency, dry run, sending, failure email + exit code."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from tests.conftest import FakeProvider, retime
from tvreport import cli
from tvreport.history import History
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

    state.update(run=run, db=db, reports=reports)
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
