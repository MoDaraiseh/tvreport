"""history.db idempotency and storage; Friday pick performance math."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from tvreport.history import History, PickRecord, RunRecord
from tvreport.performance import evaluate_picks, summarize


def run(day: date, run_type: str, bench: float | None = 7000.0) -> RunRecord:
    return RunRecord(day, run_type, "2026-11-02T13:00:00+00:00", day, "SP:SPX", bench, 1000)


def pick(list_name: str, symbol: str, ref: float, rank: int = 1) -> PickRecord:
    return PickRecord(
        list_name,
        rank,
        symbol,
        symbol.split(":")[1],
        "Co",
        "Finance",
        ref,
        70.0,
        {"s_tv": 60.0},
        "why",
    )


def test_idempotency_and_unique_constraint(tmp_path):
    h = History(tmp_path / "h.db")
    monday = date(2026, 11, 2)
    assert not h.run_exists(monday, "monday")
    h.record_run(run(monday, "monday"), [pick("BUY", "X:A", 10.0)])
    assert h.run_exists(monday, "monday")
    assert not h.run_exists(monday, "friday")
    with pytest.raises(sqlite3.IntegrityError):
        h.record_run(run(monday, "monday"), [])


def test_failed_record_leaves_nothing_behind(tmp_path):
    h = History(tmp_path / "h.db")
    with pytest.raises(ValueError):  # results without a Monday run id
        h.record_run(
            run(date(2026, 11, 6), "friday"),
            [],
            results=evaluate_picks([pick("BUY", "X:A", 10.0)], {"X:A": 11.0}, 1.0),
        )
    assert not h.run_exists(date(2026, 11, 6), "friday")  # whole transaction rolled back


def test_monday_lookup_picks_and_weekly_round_trip(tmp_path):
    h = History(tmp_path / "h.db")
    mon_id = h.record_run(
        run(date(2026, 11, 2), "monday", bench=7000.0),
        [pick("BUY", "X:A", 10.0), pick("SELL", "X:B", 20.0), pick("HOLD", "X:C", 30.0)],
    )
    found = h.monday_run_between(date(2026, 11, 2), date(2026, 11, 6))
    assert found is not None and found.id == mon_id and found.benchmark_ref == 7000.0
    assert h.monday_run_between(date(2026, 11, 9), date(2026, 11, 13)) is None
    assert [p.symbol for p in h.picks_for(mon_id)] == ["X:A", "X:B"]  # BUY and SELL only
    assert h.picks_for(mon_id)[0].subscores == {"s_tv": 60.0}

    h.record_run(
        run(date(2026, 11, 6), "friday"),
        [],
        weekly={"X:A": ("stock", 1.5), "SP:SPX": ("index", 0.7)},
        week_ending=date(2026, 11, 6),
    )
    assert h.stored_weekly(date(2026, 11, 6), "stock") == {"X:A": 1.5}
    assert h.stored_weekly(date(2026, 11, 6), "index") == {"SP:SPX": 0.7}


def test_evaluate_and_summarize_hand_calculated():
    picks = [
        pick("BUY", "X:A", 100.0),
        pick("BUY", "X:B", 50.0),
        pick("BUY", "X:M", 10.0),
        pick("SELL", "X:S", 200.0),
        pick("SELL", "X:T", 40.0),
    ]
    end = {"X:A": 105.0, "X:B": 49.0, "X:S": 190.0, "X:T": 41.0}  # X:M missing
    results = evaluate_picks(picks, end, benchmark_return_pct=1.0)
    by = {r.symbol: r for r in results}
    assert by["X:A"].return_pct == pytest.approx(5.0) and by["X:A"].beat_benchmark is True
    assert by["X:B"].return_pct == pytest.approx(-2.0) and by["X:B"].beat_benchmark is False
    assert by["X:M"].return_pct is None and by["X:M"].beat_benchmark is None
    assert by["X:S"].return_pct == pytest.approx(-5.0) and by["X:S"].beat_benchmark is True
    assert by["X:T"].return_pct == pytest.approx(2.5) and by["X:T"].beat_benchmark is False

    buy = summarize(results, "BUY")
    # measured A, B: avg (5 - 2)/2 = 1.5 ; excess (4 + -3)/2 = 0.5 ; beat 1/2 ; up 1/2
    assert (buy.picks, buy.measured) == (3, 2)
    assert buy.avg_return_pct == pytest.approx(1.5)
    assert buy.avg_excess_pct == pytest.approx(0.5)
    assert buy.hit_rate_pct == pytest.approx(50.0) and buy.up_rate_pct == pytest.approx(50.0)
    sell = summarize(results, "SELL")
    # S -5, T +2.5: avg -1.25 ; SELL "up rate" = share that fell = 1/2
    assert sell.avg_return_pct == pytest.approx(-1.25) and sell.up_rate_pct == pytest.approx(50.0)


def test_no_benchmark_means_no_hit_rate():
    results = evaluate_picks([pick("BUY", "X:A", 100.0)], {"X:A": 110.0}, None)
    stats = summarize(results, "BUY")
    assert stats.avg_return_pct == pytest.approx(10.0)
    assert stats.hit_rate_pct is None and stats.avg_excess_pct is None


def test_cumulative_results_exclude_current_friday(tmp_path):
    h = History(tmp_path / "h.db")
    mon = h.record_run(run(date(2026, 11, 2), "monday"), [pick("BUY", "X:A", 100.0)])
    res = evaluate_picks([pick("BUY", "X:A", 100.0)], {"X:A": 103.0}, 1.0)
    h.record_run(run(date(2026, 11, 6), "friday"), [], results=res, monday_run_id=mon)
    assert len(h.all_results()) == 1
    assert h.all_results(excluding_friday_on=date(2026, 11, 6)) == []
