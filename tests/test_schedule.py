"""Schedule gating across both DST changes (Nov 1 2026, Mar 14 2027), holidays,
early closes, and the workflow's actual cron lines."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from scripts.pregate import decide as pregate_decide

from tvreport.schedule import (
    NY,
    RunType,
    friday_earliest,
    gate,
    resolve_run_type,
    session_context,
)

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "weekly-report.yml"


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def ny(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=NY)


@pytest.mark.parametrize(
    ("when", "run_type", "proceed"),
    [
        # Friday before the DST change (EDT): 20:45 UTC = 16:45 ET
        ("2026-10-30 20:45", "friday", True),
        ("2026-10-30 22:15", "friday", True),  # 18:15 ET, last minute
        ("2026-10-30 22:30", "friday", False),  # 18:30 ET
        # Friday after the change (EST): 20:45 UTC = 15:45 ET -- before the close
        ("2026-11-06 20:45", "friday", False),
        ("2026-11-06 21:30", "friday", True),  # 16:30 ET = close + 30
        ("2026-11-06 21:45", "friday", True),
        # Monday after the change (EST): 12:00 UTC = 07:00 ET, 13:00 UTC = 08:00 ET
        ("2026-11-02 12:00", "monday", True),
        ("2026-11-02 13:00", "monday", True),
        ("2026-11-02 14:25", "monday", True),  # 09:25 ET
        ("2026-11-02 14:30", "monday", False),  # 09:30 ET: the open
        ("2026-11-02 11:15", "monday", False),  # 06:15 ET
        # Monday before the spring change (EST) and after it (EDT)
        ("2027-03-08 13:00", "monday", True),
        ("2027-03-15 12:00", "monday", True),  # 08:00 EDT
        ("2027-03-15 14:00", "monday", False),  # 10:00 EDT
        ("2027-03-12 21:45", "friday", True),  # 16:45 EST
        ("2027-03-19 20:45", "friday", True),  # 16:45 EDT
        # Wrong weekday
        ("2026-11-03 13:00", "monday", False),
    ],
)
def test_gate(when, run_type, proceed, cfg, calendar):
    assert gate(run_type, utc(when), cfg.schedule, calendar).proceed is proceed


def test_monday_window_edges(cfg, calendar):
    assert not gate("monday", ny("2026-11-02 06:29"), cfg.schedule, calendar).proceed
    assert gate("monday", ny("2026-11-02 06:30"), cfg.schedule, calendar).proceed
    assert gate("monday", ny("2026-11-02 09:25"), cfg.schedule, calendar).proceed
    assert not gate("monday", ny("2026-11-02 09:26"), cfg.schedule, calendar).proceed


def test_early_close_friday_window(cfg, calendar):
    # Fri 27 Nov 2026 closes 13:00 ET -> window opens 13:30
    assert friday_earliest(date(2026, 11, 27), cfg.schedule, calendar) == ny("2026-11-27 13:30")
    assert not gate("friday", ny("2026-11-27 13:29"), cfg.schedule, calendar).proceed
    assert gate("friday", ny("2026-11-27 13:30"), cfg.schedule, calendar).proceed
    ctx = session_context("friday", ny("2026-11-27 16:45"), calendar)
    assert ctx.early_close_today and ctx.expected_data_session == date(2026, 11, 27)
    assert any("early-close" in n and "13:00" in n for n in ctx.notes)


def test_good_friday_holiday_still_runs_with_label(cfg, calendar):
    now = ny("2027-03-26 16:45")
    assert gate("friday", now, cfg.schedule, calendar).proceed
    ctx = session_context("friday", now, calendar)
    assert not ctx.market_open_today
    assert ctx.expected_data_session == date(2027, 3, 25)
    assert any("closed today" in n and "Thu 25 Mar" in n for n in ctx.notes)


def test_monday_holiday_still_runs_and_says_closed(cfg, calendar):
    now = ny("2027-01-18 08:00")  # MLK day
    assert gate("monday", now, cfg.schedule, calendar).proceed
    ctx = session_context("monday", now, calendar)
    assert not ctx.market_open_today
    assert ctx.expected_data_session == date(2027, 1, 15)
    assert any("closed today" in n for n in ctx.notes)


def test_monday_after_friday_holiday_uses_thursday(calendar):
    ctx = session_context("monday", ny("2027-03-29 08:00"), calendar)  # after Good Friday
    assert ctx.expected_data_session == date(2027, 3, 25)
    assert ctx.last_week_end_session == date(2027, 3, 25)


def test_resolve_run_type():
    assert resolve_run_type("auto", ny("2026-11-02 08:00")) == "monday"
    assert resolve_run_type("auto", ny("2026-11-06 16:45")) == "friday"
    assert resolve_run_type("auto", ny("2026-11-04 08:00")) is None
    assert resolve_run_type("friday", ny("2026-11-04 08:00")) == "friday"
    with pytest.raises(ValueError):
        resolve_run_type("tuesday", ny("2026-11-04 08:00"))


# ------------------------------------------------------------------ workflow crons
def _expand(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for raw_part in field.split(","):
        part, _, step_text = raw_part.partition("/")
        step = int(step_text) if step_text else 1
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start, end = (int(x) for x in part.split("-"))
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def workflow_triggers() -> dict[int, list[time]]:
    """UTC trigger times per weekday (0 = Monday) from the workflow's cron lines."""

    text = WORKFLOW.read_text(encoding="utf-8")
    out: dict[int, list[time]] = {}
    for minute, hour, _dom, _mon, dow in re.findall(r'cron: "(\S+) (\S+) (\S+) (\S+) (\S+)"', text):
        for d in _expand(dow, 0, 6):
            weekday = (d - 1) % 7  # cron: 0 = Sunday
            out.setdefault(weekday, []).extend(
                time(h, m) for h in _expand(hour, 0, 23) for m in _expand(minute, 0, 59)
            )
    return out


def test_crons_cover_every_window_across_dst(cfg, calendar):
    triggers = workflow_triggers()
    day = date(2026, 10, 26)  # a Monday, before the Nov 1 change; runs past the Mar 14 change
    while day <= date(2027, 4, 2):
        cases: tuple[tuple[int, RunType, time], ...] = (
            (0, "monday", time(8, 0)),
            (4, "friday", time(16, 45)),
        )
        for weekday, run_type, target in cases:
            current = day + timedelta(days=weekday)
            stamps = [datetime.combine(current, t, UTC) for t in triggers[weekday]]
            accepted = [s for s in stamps if gate(run_type, s, cfg.schedule, calendar).proceed]
            assert accepted, f"no accepted {run_type} trigger on {current}"
            local_times = {s.astimezone(NY).time() for s in accepted}
            assert target in local_times, f"{current}: no trigger at {target} ET"
            if run_type == "friday":
                earliest = friday_earliest(current, cfg.schedule, calendar)
                assert all(s.astimezone(NY) >= earliest for s in accepted)
            for s in accepted:  # the cheap pre-check must never reject a real run
                assert pregate_decide(s, Path("does-not-exist.db"))[0], s
        day += timedelta(days=7)
