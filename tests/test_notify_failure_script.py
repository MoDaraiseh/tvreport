"""scripts/notify_failure.py: stdlib failure email, rate-limited per window and
sharing the alerts table with tvreport.history."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from scripts import notify_failure

from tvreport.history import History

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "me@example.com",
    "SMTP_PASSWORD": "s3cret-app-password",
    "REPORT_TO": "you@example.com",
}


@pytest.fixture
def sent(monkeypatch):
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(notify_failure, "send_email", lambda s, b: messages.append((s, b)))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    return messages


def test_one_email_per_run_type_per_window(sent, tmp_path):
    db = tmp_path / "data" / "history.db"
    argv = ["notify_failure.py", "friday", "Install", "https://example.com/run/1"]
    assert notify_failure.main(argv, db) == 0
    assert notify_failure.main(argv, db) == 0
    assert len(sent) == 1 and sent[0][0].startswith("tvreport FAILED — friday ")
    assert notify_failure.main(["x", "monday", "Install", "u"], db) == 0
    assert len(sent) == 2


def test_shares_alert_records_with_history(sent, tmp_path):
    db = tmp_path / "history.db"
    history = History(db)  # schema created by the package
    today = datetime.now(UTC).astimezone(notify_failure.NY).date()
    history.record_alert(today, "friday", "2026-11-06T21:00:00+00:00")
    assert notify_failure.main(["x", "friday", "Install", "u"], db) == 0
    assert sent == []  # the CLI already alerted for this window
    assert notify_failure.alert_already_sent(db, today, "friday")


def test_script_records_are_visible_to_history(sent, tmp_path):
    db = tmp_path / "history.db"
    assert notify_failure.main(["x", "monday", "Install", "u"], db) == 0
    today = datetime.now(UTC).astimezone(notify_failure.NY).date()
    assert History(db).alert_already_sent(today, "monday")
    assert not History(db).alert_already_sent(date(2000, 1, 1), "monday")
