"""Schedule gating across both DST changes (Nov 1 2026, Mar 14 2027), holidays,
early closes, and the workflow's actual cron lines."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from tests.workflow_gate import run_gate
from tvreport.schedule import (
    NY,
    GateDecision,
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
        ("2026-10-30 22:30", "friday", True),  # 18:30 ET: catch-up period, [DELAYED]
        # Friday after the change (EST): 20:45 UTC = 15:45 ET -- before the close
        ("2026-11-06 20:45", "friday", False),
        ("2026-11-06 21:30", "friday", True),  # 16:30 ET = close + 30
        ("2026-11-06 21:45", "friday", True),
        # Monday after the change (EST): 12:00 UTC = 07:00 ET, 13:00 UTC = 08:00 ET
        ("2026-11-02 12:00", "monday", True),
        ("2026-11-02 13:00", "monday", True),
        ("2026-11-02 14:25", "monday", True),  # 09:25 ET
        ("2026-11-02 14:30", "monday", True),  # 09:30 ET: the open; catch-up period
        ("2026-11-02 21:01", "monday", False),  # 16:01 ET: after the catch-up cutoff
        ("2026-11-02 11:15", "monday", False),  # 06:15 ET
        # Monday before the spring change (EST) and after it (EDT)
        ("2027-03-08 13:00", "monday", True),
        ("2027-03-15 12:00", "monday", True),  # 08:00 EDT
        ("2027-03-15 14:00", "monday", True),  # 10:00 EDT: catch-up period
        ("2027-03-15 20:01", "monday", False),  # 16:01 EDT
        ("2027-03-12 21:45", "friday", True),  # 16:45 EST
        ("2027-03-19 20:45", "friday", True),  # 16:45 EDT
        # Wrong weekday
        ("2026-11-03 13:00", "monday", False),
    ],
)
def test_gate(when, run_type, proceed, cfg, calendar):
    assert gate(run_type, utc(when), cfg.schedule, calendar).proceed is proceed


def test_monday_window_edges(cfg, calendar):
    def at(s: str) -> GateDecision:
        return gate("monday", ny(s), cfg.schedule, calendar)

    assert not at("2026-11-02 06:29").proceed
    assert at("2026-11-02 06:30").proceed and not at("2026-11-02 06:30").delayed
    assert at("2026-11-02 09:25").proceed and not at("2026-11-02 09:25").delayed
    # after the window: catch-up until 16:00 ET, marked delayed
    assert at("2026-11-02 09:26").proceed and at("2026-11-02 09:26").delayed
    assert at("2026-11-02 16:00").proceed and at("2026-11-02 16:00").delayed
    assert not at("2026-11-02 16:01").proceed


def test_friday_catch_up_edges(cfg, calendar):
    def at(s: str) -> GateDecision:
        return gate("friday", ny(s), cfg.schedule, calendar)

    assert at("2026-11-06 18:15").proceed and not at("2026-11-06 18:15").delayed
    assert at("2026-11-06 18:16").proceed and at("2026-11-06 18:16").delayed
    assert at("2026-11-06 23:59").proceed and at("2026-11-06 23:59").delayed
    assert not at("2026-11-07 00:00").proceed  # Saturday
    # early-close Friday: still no run before close + 30, catch-up after 18:15
    assert not at("2026-11-27 13:29").proceed
    assert at("2026-11-27 20:00").delayed


def test_catch_up_cutoff_comes_from_config(cfg, calendar):
    schedule = cfg.schedule.model_copy(
        update={"monday": cfg.schedule.monday.model_copy(update={"catch_up_until": time(9, 25)})}
    )
    assert not gate("monday", ny("2026-11-02 09:26"), schedule, calendar).proceed


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
            for s in accepted:  # the workflow's first-step gate must never reject a real run
                assert run_gate(s) == {"run": "true", "run_type": run_type, "missed": ""}, s
        day += timedelta(days=7)


def test_workflow_gate_rejects_outside_windows():
    for when in (
        "2026-11-02 21:01",  # Monday 16:01 EST, after the catch-up cutoff
        "2026-11-02 10:15",
        "2026-11-04 13:00",
        "2026-11-06 18:00",  # 13:00 EST, before 13:30
        "2026-11-07 05:00",  # Saturday 00:00 EST
        "2026-11-07 21:45",
    ):
        assert run_gate(utc(when))["run"] == "false", when


def test_workflow_gate_manual_runs():
    wednesday = utc("2026-11-04 15:00")
    assert run_gate(wednesday, "workflow_dispatch", "friday") == {
        "run": "true",
        "run_type": "friday",
        "missed": "",
    }
    assert run_gate(wednesday, "workflow_dispatch", "auto")["run"] == "false"  # auto = gated


def test_workflow_test_email_option():
    wednesday = utc("2026-11-04 15:00")
    assert run_gate(wednesday, "workflow_dispatch", "test_email") == {
        "run": "true",
        "run_type": "test_email",
        "missed": "",
    }
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m tvreport test-email" in text
    steps = {}
    current = None
    for line in text.splitlines():
        if line.strip().startswith("- name:"):
            current = line.split("- name:", 1)[1].strip()
        elif current and line.strip().startswith("if:"):
            steps[current] = line.split("if:", 1)[1]
    # the test email must never commit history or send/record a failure alert
    assert "inputs.run_type == 'test_email'" in steps["Commit history and reports"]
    assert (
        "inputs.run_type != 'test_email'"
        in steps["Failure email for errors outside the report step"]
    )


# ------------------------------------------------------------------ late triggers
# The 15 scheduled runs GitHub created between 30 Oct and 14 Nov 2026, all after
# their window had closed (created_at, UTC). Each one used to end green.
# "catch_up": inside the catch-up period, so it builds a [DELAYED] report;
# "missed": after the cutoff (Monday 16:00 ET).
LATE_TRIGGERS = [
    ("2026-10-30 23:57", "friday", "catch_up"),  # Fri 19:57 EDT
    ("2026-10-31 00:24", "friday", "catch_up"),
    ("2026-10-31 02:02", "friday", "catch_up"),
    ("2026-11-02 20:25", "monday", "catch_up"),  # Mon 15:25 EST
    ("2026-11-02 20:38", "monday", "catch_up"),
    ("2026-11-02 22:38", "monday", "missed"),  # Mon 17:38 EST
    ("2026-11-07 00:13", "friday", "catch_up"),  # Fri 19:13 EST
    ("2026-11-07 00:38", "friday", "catch_up"),
    ("2026-11-07 02:33", "friday", "catch_up"),
    ("2026-11-09 20:52", "monday", "catch_up"),  # Mon 15:52 EST
    ("2026-11-09 21:18", "monday", "missed"),  # Mon 16:18 EST
    ("2026-11-09 23:18", "monday", "missed"),
    ("2026-11-14 00:58", "friday", "catch_up"),
    ("2026-11-14 01:17", "friday", "catch_up"),
    ("2026-11-14 02:54", "friday", "catch_up"),
]


@pytest.mark.parametrize(("when", "run_type", "outcome"), LATE_TRIGGERS)
def test_workflow_gate_on_late_scheduled_triggers(when, run_type, outcome, cfg, calendar):
    if outcome == "catch_up":
        assert run_gate(utc(when)) == {"run": "true", "run_type": run_type, "missed": ""}
        decision = gate(run_type, utc(when), cfg.schedule, calendar)
        assert decision.proceed and decision.delayed  # the CLI agrees
    else:
        assert run_gate(utc(when)) == {"run": "false", "run_type": "", "missed": run_type}
        assert not gate(run_type, utc(when), cfg.schedule, calendar).proceed


def test_first_late_trigger_of_each_window_would_have_caught_up():
    first: dict[str, str] = {}
    for when, _run_type, outcome in LATE_TRIGGERS:
        first.setdefault(utc(when).astimezone(NY).date().isoformat(), outcome)
    assert len(first) == 5 and set(first.values()) == {"catch_up"}


def test_workflow_gate_missed_only_after_the_cutoff_on_the_same_day():
    for when in (
        "2026-11-02 11:15",  # Monday 06:15 EST: before the window
        "2026-11-02 14:26",  # Monday 09:26 EST: catch-up period
        "2026-11-06 18:00",  # Friday 13:00 EST: before the earliest start
        "2026-11-06 23:16",  # Friday 18:16 EST: catch-up period
        "2026-11-04 23:00",  # Wednesday
        "2026-11-07 12:00",  # Saturday morning in New York
    ):
        assert run_gate(utc(when))["missed"] == "", when
    assert run_gate(utc("2026-11-02 21:01"))["missed"] == "monday"  # 16:01 EST
    assert run_gate(utc("2026-11-07 04:59:30"))["missed"] == "friday"  # 23:59:30 EST


def test_workflow_gate_missed_for_dispatch():
    late_monday = utc("2026-11-09 21:18")  # 16:18 EST
    # an outside scheduler's `auto` call that would send email counts as missed
    assert run_gate(late_monday, "workflow_dispatch", "auto", "false")["missed"] == "monday"
    # a dry run never does
    assert run_gate(late_monday, "workflow_dispatch", "auto", "true")["missed"] == ""
    # a manual monday/friday run ignores the window entirely
    assert run_gate(late_monday, "workflow_dispatch", "monday", "false") == {
        "run": "true",
        "run_type": "monday",
        "missed": "",
    }


def _step_conditions() -> dict[str, str]:
    steps: dict[str, str] = {}
    current = None
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = stripped.split("- name:", 1)[1].strip()
        elif stripped.startswith("- "):  # an unnamed step, e.g. `- uses: ...`
            current = None
        elif current and stripped.startswith("if:") and current not in steps:
            steps[current] = stripped.split("if:", 1)[1].strip()
    return steps


def test_workflow_missed_window_fails_and_alerts_once():
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = _step_conditions()
    assert steps["Missed window"] == (
        "steps.gate.outputs.missed != '' && steps.sent.outputs.sent != 'true'"
    )
    block = text.split("- name: Missed window", 1)[1].split("\n      - ", 1)[0]
    # fails the first late trigger per window, then ends quietly (tests/test_missed_window.py)
    assert "python3 -m scripts.missed_window" in block
    assert "|| true" not in block
    # the alert record is committed, so later late triggers in the window don't re-email
    assert "steps.gate.outputs.missed != ''" in steps["Commit history and reports"]
    # the already-sent check runs for a missed trigger, so a sent report stays green
    assert "steps.gate.outputs.missed != ''" in steps["Already-sent check"]


def test_workflow_keeps_schedule_enabled_on_every_trigger():
    assert _step_conditions()["Keep schedule enabled"] == "always()"
    assert "actions: write" in WORKFLOW.read_text(encoding="utf-8")
