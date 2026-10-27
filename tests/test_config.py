"""Startup validation of config.yaml, and the workflow's stdlib pre-check."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from scripts.already_sent import already_sent

from tests.workflow_gate import run_gate
from tvreport.config import Config, Weights, load_config
from tvreport.history import History, RunRecord
from tvreport.report import md_cell
from tvreport.schedule import NY


def test_shipped_config_is_valid():
    cfg = load_config()
    assert sum(cfg.weights.model_dump().values()) == pytest.approx(1.0)
    assert cfg.benchmark_symbol == "SP:SPX"
    assert {i.symbol for i in cfg.indices} == {
        "SP:SPX",
        "NASDAQ:IXIC",
        "NASDAQ:NDX",
        "DJ:DJI",
        "FTSE:RUT",
        "CBOE:VIX",
    }


def test_weights_must_sum_to_one():
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        Weights(tv_rating=0.5)
    Weights(tv_rating=0.2, trend=0.2, momentum=0.2, volume=0.2, range_52w=0.2)


def test_rejects_unknown_keys_and_bad_values():
    base = load_config().model_dump()
    with pytest.raises(ValidationError):
        Config.model_validate({**base, "typo_key": 1})
    with pytest.raises(ValidationError, match="before the 09:30"):
        Config.model_validate(
            {
                **base,
                "schedule": {
                    **base["schedule"],
                    "monday": {"target": "08:00", "earliest": "06:30", "latest": "09:45"},
                },
            }
        )
    with pytest.raises(ValidationError, match="benchmark_symbol"):
        Config.model_validate({**base, "benchmark_symbol": "NOPE:X"})
    with pytest.raises(ValidationError, match="min_size"):
        Config.model_validate({**base, "universe": {**base["universe"], "min_size": 5000}})


def test_already_sent_reads_history(tmp_path):
    db = tmp_path / "history.db"
    assert already_sent("monday", date(2026, 11, 2), db) is False  # no file yet
    History(db).record_run(
        RunRecord(date(2026, 11, 2), "monday", "x", date(2026, 10, 30), "SP:SPX", 1.0, 1000), []
    )
    assert already_sent("monday", date(2026, 11, 2), db) is True
    assert already_sent("friday", date(2026, 11, 2), db) is False
    assert already_sent("monday", date(2026, 11, 9), db) is False


def test_workflow_gate_windows_cover_config_windows():
    """The workflow's first-step gate hard-codes its windows (it runs before any
    install); they must stay a superset of config.yaml's or real runs would be
    silently blocked."""

    sched = load_config().schedule
    monday = datetime(2026, 11, 2, tzinfo=NY)
    assert run_gate(datetime.combine(monday, sched.monday.earliest, NY))["run"] == "true"
    assert run_gate(datetime.combine(monday, sched.monday.latest, NY))["run"] == "true"
    friday = datetime(2026, 11, 27, tzinfo=NY)  # an early close (13:00 ET)
    earliest = datetime(2026, 11, 27, 13, 0, tzinfo=NY) + timedelta(
        minutes=sched.friday.minutes_after_close
    )
    assert run_gate(earliest)["run"] == "true"
    assert run_gate(datetime.combine(friday, sched.friday.latest, NY))["run"] == "true"


def test_markdown_cells_are_escaped():
    assert md_cell("A | B\nC") == r"A \| B C"
    assert md_cell(None) == ""
