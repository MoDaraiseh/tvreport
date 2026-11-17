"""scripts/missed_window.py: only the first late trigger after the catch-up cutoff
fails the run and alerts; later ones in the same window end quietly."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from scripts import missed_window, notify_failure

from tvreport.history import MISSED_DDL, History

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "me@example.com",
    "SMTP_PASSWORD": "s3cret-app-password",
    "REPORT_TO": "you@example.com",
}
LATE_MONDAY = datetime(2026, 11, 9, 21, 18, tzinfo=UTC)  # 16:18 EST, after the cutoff


@pytest.fixture
def sent(monkeypatch):
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(notify_failure, "send_email", lambda s, b: messages.append((s, b)))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    return messages


def test_first_late_trigger_fails_and_alerts_later_ones_are_quiet(sent, tmp_path, capsys):
    db = tmp_path / "data" / "history.db"
    argv = ["missed_window.py", "monday", "https://example.com/run/1"]
    assert missed_window.main(argv, db, now=LATE_MONDAY) == 1
    assert "::error::" in capsys.readouterr().out
    assert len(sent) == 1 and sent[0][0].startswith("tvreport FAILED — monday ")
    assert "catch-up cutoff" in sent[0][1]
    for _ in range(2):  # the next late triggers in the same window
        assert missed_window.main(argv, db, now=LATE_MONDAY) == 0
    assert len(sent) == 1
    assert "::error::" not in capsys.readouterr().out


def test_noted_even_without_smtp(monkeypatch, tmp_path):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "history.db"
    argv = ["missed_window.py", "monday", "u"]
    assert missed_window.main(argv, db, now=LATE_MONDAY) == 1  # alert can't send: still red once
    assert missed_window.main(argv, db, now=LATE_MONDAY) == 0
    assert missed_window.already_noted(
        db, LATE_MONDAY.astimezone(missed_window.NY).date(), "monday"
    )


def test_windows_are_separate(sent, tmp_path):
    db = tmp_path / "history.db"
    assert missed_window.main(["x", "monday", "u"], db, now=LATE_MONDAY) == 1
    next_monday = datetime(2026, 11, 16, 21, 18, tzinfo=UTC)
    assert missed_window.main(["x", "monday", "u"], db, now=next_monday) == 1


def test_rejects_bad_run_type(tmp_path):
    assert missed_window.main(["x", "tuesday"], tmp_path / "h.db") == 2


def test_same_table_as_history(tmp_path):
    assert missed_window.MISSED_DDL == MISSED_DDL
    db = tmp_path / "history.db"
    History(db)  # the package creates the table; the script reads it
    missed_window.note(db, LATE_MONDAY.date(), "monday", LATE_MONDAY)
    assert missed_window.already_noted(db, LATE_MONDAY.date(), "monday")
