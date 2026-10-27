"""Startup validation of config.yaml, and the workflow's stdlib pre-check."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from scripts.pregate import WINDOWS, decide

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


def test_pregate_skips_already_recorded_run(tmp_path):
    now = datetime(2026, 11, 2, 8, 0, tzinfo=NY)
    db = tmp_path / "history.db"
    assert decide(now, db)[0] is True
    History(db).record_run(
        RunRecord(date(2026, 11, 2), "monday", "x", date(2026, 10, 30), "SP:SPX", 1.0, 1000), []
    )
    run, reason = decide(now, db)
    assert run is False and "already recorded" in reason
    assert decide(datetime(2026, 11, 6, 16, 45, tzinfo=NY), db)[0] is True  # Friday still due


def test_pregate_windows_cover_config_windows():
    """scripts/pregate.py is stdlib-only and hard-codes its windows; it must stay a
    superset of config.yaml's, or it would silently block real runs."""

    sched = load_config().schedule
    _, mon_start, mon_end = WINDOWS[0]
    assert mon_start <= sched.monday.earliest and mon_end >= sched.monday.latest
    _, fri_start, fri_end = WINDOWS[4]
    earliest_close = datetime(2026, 11, 27, 13, 0)  # NYSE early closes are at 13:00 ET
    earliest_run = (earliest_close + timedelta(minutes=sched.friday.minutes_after_close)).time()
    assert fri_start <= earliest_run and fri_end >= sched.friday.latest


def test_markdown_cells_are_escaped():
    assert md_cell("A | B\nC") == r"A \| B C"
    assert md_cell(None) == ""
