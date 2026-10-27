"""End to end with recorded data: Monday run -> history -> Friday evaluation;
report rendering and email construction (nothing is sent)."""

from __future__ import annotations

import smtplib
from datetime import date, datetime
from email.message import EmailMessage

import pandas as pd
import pytest

from tests.conftest import FakeProvider, retime, stock_row
from tvreport.history import History
from tvreport.notify import build_message, failure_message, scrub, send, smtp_settings
from tvreport.pipeline import build_report
from tvreport.report import render_html, render_markdown, render_text, save_report
from tvreport.schedule import NY

MON = datetime(2026, 11, 2, 8, 0, tzinfo=NY)
FRI = datetime(2026, 11, 6, 16, 45, tzinfo=NY)
ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "me@example.com",
    "SMTP_PASSWORD": "s3cret-app-password",
    "REPORT_TO": "you@example.com",
}


def monday_data(
    universe_raw: pd.DataFrame, indices_raw: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        retime(universe_raw, date(2026, 10, 30), date(2026, 10, 26)),
        retime(indices_raw, date(2026, 10, 30), date(2026, 10, 26)),
    )


def record(history: History, outcome) -> None:
    history.record_run(
        outcome.run_record,
        outcome.picks,
        weekly=outcome.weekly_to_store or None,
        week_ending=outcome.run_record.data_session if outcome.weekly_to_store else None,
        results=outcome.pick_results,
        monday_run_id=outcome.monday_run.id if outcome.monday_run else None,
    )


@pytest.fixture
def monday_outcome(universe_raw, indices_raw, cfg, calendar, tmp_path):
    history = History(tmp_path / "h.db")
    universe, indices = monday_data(universe_raw, indices_raw)
    outcome = build_report(
        "monday",
        MON,
        cfg,
        FakeProvider(universe, indices),
        history,
        dry_run=False,
        holdings_path=tmp_path / "none.yaml",
        calendar=calendar,
    )
    return history, outcome


def test_monday_report(monday_outcome, cfg):
    _, outcome = monday_outcome
    report = outcome.report
    assert report.subject == "Week Ahead — Mon 2 Nov 2026"
    assert len(report.buy) == cfg.classify.n_buy and len(report.sell) == cfg.classify.n_sell
    assert 0 < len(report.hold) <= cfg.classify.n_hold
    listed = [r["symbol"] for r in report.buy + report.sell + report.hold]
    assert len(listed) == len(set(listed))
    sectors = pd.Series([r["sector"] for r in report.buy]).value_counts()
    assert sectors.max() <= cfg.classify.max_per_sector
    assert [i.name for i in report.overview.indices] == [i.name for i in cfg.indices]
    assert all(i.level and i.weekly_pct is not None for i in report.overview.indices)
    assert report.overview.weekly_source == "tradingview"
    assert report.extra["weekly_label"].startswith("last week, ending Fri 30 Oct")
    assert report.overview.breadth.measured > 0 and len(report.overview.gainers) == 10
    assert report.performance is None
    assert outcome.run_record.benchmark_ref == pytest.approx(
        next(i.level for i in report.overview.indices if i.name == "S&P 500")
    )
    assert any("without a 200-day SMA" in n for n in report.data_notes)


def test_friday_evaluates_mondays_picks(monday_outcome, universe_raw, indices_raw, cfg, calendar):
    history, monday = monday_outcome
    record(history, monday)
    buys = {r["symbol"] for r in monday.report.buy}
    sells = {r["symbol"] for r in monday.report.sell}

    universe = retime(universe_raw, date(2026, 11, 6), date(2026, 11, 2))
    ref = universe.set_index("symbol")["close"]
    universe.loc[universe["symbol"].isin(buys), "close"] = (
        universe["symbol"].map(ref) * 1.05
    )  # every BUY +5%
    universe.loc[universe["symbol"].isin(sells), "close"] = (
        universe["symbol"].map(ref) * 0.95
    )  # every SELL -5%
    indices = retime(indices_raw, date(2026, 11, 6), date(2026, 11, 2))
    spx = indices["symbol"] == "SP:SPX"
    indices.loc[spx, "close"] = monday.run_record.benchmark_ref * 1.01  # S&P +1%
    indices.loc[spx, "prev_close"] = monday.run_record.benchmark_ref
    rut = indices["symbol"] == "FTSE:RUT"
    indices.loc[rut, "prev_close"] = indices.loc[rut, "close"]  # official close not published yet

    # Monday reference prices must equal the Monday closes, so returns are exactly +/-5%.
    outcome = build_report(
        "friday",
        FRI,
        cfg,
        FakeProvider(universe, indices),
        history,
        dry_run=False,
        holdings_path=None,
        calendar=calendar,
    )
    perf = outcome.report.performance
    assert perf is not None and perf.monday_run_date == date(2026, 11, 2)
    assert perf.benchmark_return_pct == pytest.approx(1.0)
    assert len(perf.rows) == len(buys) + len(sells)
    for row in perf.rows:
        expected = 5.0 if row["list"] == "BUY" else -5.0
        assert row["return_pct"] == pytest.approx(expected) and row["beat"] is True
    buy_stats, sell_stats = perf.week_stats
    assert buy_stats.hit_rate_pct == 100 and sell_stats.hit_rate_pct == 100
    assert buy_stats.avg_excess_pct == pytest.approx(4.0)

    russell = next(i for i in outcome.report.overview.indices if i.name == "Russell 2000")
    assert russell.symbol == "TVC:RUT" and "FTSE:RUT" in (russell.note or "")
    assert outcome.report.subject == "Week in Review — Fri 6 Nov 2026"
    assert outcome.weekly_to_store and "SP:SPX" in outcome.weekly_to_store

    record(history, outcome)
    assert len(history.all_results()) == len(perf.rows)


def test_friday_without_monday_run(universe_raw, indices_raw, cfg, calendar, tmp_path):
    universe = retime(universe_raw, date(2026, 11, 6), date(2026, 11, 2))
    indices = retime(indices_raw, date(2026, 11, 6), date(2026, 11, 2))
    outcome = build_report(
        "friday",
        FRI,
        cfg,
        FakeProvider(universe, indices),
        History(tmp_path / "h.db"),
        dry_run=True,
        calendar=calendar,
    )
    assert outcome.report.performance is not None
    assert "No Monday report" in (outcome.report.performance.note or "")


def test_monday_after_roll_without_history_says_unavailable(
    universe_raw, indices_raw, cfg, calendar, tmp_path
):
    universe = retime(universe_raw, date(2026, 10, 30), date(2026, 11, 2))  # weekly bar rolled
    indices = retime(indices_raw, date(2026, 10, 30), date(2026, 11, 2))
    outcome = build_report(
        "monday",
        MON,
        cfg,
        FakeProvider(universe, indices),
        History(tmp_path / "h.db"),
        dry_run=True,
        calendar=calendar,
    )
    assert outcome.report.overview.weekly_source == "unavailable"
    assert "unavailable" in outcome.report.extra["weekly_label"]
    assert all(r["weekly_pct"] is None for r in outcome.report.buy)
    assert "unavailable" in render_text(outcome.report)


def test_portfolio_mode(universe_raw, indices_raw, cfg, calendar, tmp_path):
    universe, indices = monday_data(universe_raw, indices_raw)
    outside_ok = stock_row(
        symbol="NYSE:OUTS", ticker="OUTS", sector="Utilities", bar_time=universe["bar_time"].iloc[0]
    )
    outside_bad = stock_row(symbol="AMEX:SPY", ticker="SPY", sector=None, market_cap=None)
    stocks = pd.concat([universe, pd.DataFrame([outside_ok, outside_bad])], ignore_index=True)
    holdings = tmp_path / "holdings.yaml"
    holdings.write_text(
        "holdings:\n"
        "  - {ticker: AAPL, shares: 10, avg_cost: 200}\n"
        "  - {ticker: NYSE:OUTS, shares: 5}\n"
        "  - {ticker: SPY, shares: 1}\n"
        "  - {ticker: NOPE, shares: 1}\n",
        encoding="utf-8",
    )
    outcome = build_report(
        "monday",
        MON,
        cfg,
        FakeProvider(universe, indices, stocks),
        History(tmp_path / "h.db"),
        dry_run=True,
        holdings_path=holdings,
        calendar=calendar,
    )
    rows = {r["ticker"]: r for r in outcome.report.portfolio or []}
    aapl = rows["AAPL"]
    assert aapl["in_universe"] and aapl["action"] in {"BUY MORE", "HOLD", "SELL"}
    assert aapl["pnl"] == pytest.approx((aapl["price"] - 200) * 10)
    assert (
        rows["OUTS"]["action"] in {"BUY MORE", "HOLD", "SELL"} and not rows["OUTS"]["in_universe"]
    )
    assert rows["SPY"]["action"] == "N/A" and "missing data" in rows["SPY"]["note"]
    assert rows["NOPE"]["action"] == "N/A" and "not returned" in rows["NOPE"]["note"]
    score = aapl["score"]
    expected = "BUY MORE" if score >= 65 else "SELL" if score < 40 else "HOLD"
    assert aapl["action"] == expected
    for render in (render_html, render_text, render_markdown):
        assert "NOPE" in render(outcome.report)


def test_rendering_and_files(monday_outcome, tmp_path):
    _, outcome = monday_outcome
    report = outcome.report
    html, text, md = render_html(report), render_text(report), render_markdown(report)
    for doc in (html, text, md):
        assert "Not financial advice" in doc
        assert "Mon 02 Nov 2026 08:00 ET" in doc and "Mon 02 Nov 2026 17:00 Dubai" in doc
        assert report.buy[0]["ticker"] in doc
    assert html.lstrip().startswith("<!DOCTYPE html>") and "<style" not in html  # inline CSS only
    assert 'name="viewport"' in html
    html_path, md_path = save_report(report, tmp_path / "reports")
    assert html_path.name == "2026-11-02-monday.html" and md_path.name == "2026-11-02-monday.md"
    assert md_path.read_text(encoding="utf-8").startswith("# Week Ahead — Mon 2 Nov 2026")


def test_html_escapes_untrusted_text(monday_outcome):
    _, outcome = monday_outcome
    outcome.report.buy[0]["description"] = "<script>alert(1)</script>"
    assert "<script>alert(1)</script>" not in render_html(outcome.report)


def test_email_construction_and_secrets(monday_outcome):
    _, outcome = monday_outcome
    settings = smtp_settings(ENV)
    assert "s3cret" not in repr(settings)
    msg = build_message(
        settings, outcome.report.subject, render_text(outcome.report), render_html(outcome.report)
    )
    assert msg["Subject"] == "Week Ahead — Mon 2 Nov 2026"
    assert msg["To"] == "you@example.com" and msg["From"] == "me@example.com"
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]

    failure = failure_message(
        settings, "monday 2026-11-02", RuntimeError("login failed for s3cret-app-password")
    )
    body = failure.get_content()
    assert "s3cret" not in body and "***" in body
    assert failure["Subject"] == "tvreport FAILED — monday 2026-11-02"


def test_smtp_settings_validation():
    with pytest.raises(Exception, match="SMTP_PASSWORD") as exc:
        smtp_settings({k: v for k, v in ENV.items() if k != "SMTP_PASSWORD"})
    assert "s3cret" not in str(exc.value)
    with pytest.raises(Exception, match="SMTP_PORT"):
        smtp_settings({**ENV, "SMTP_PORT": "abc"})
    assert smtp_settings({**ENV, "REPORT_TO": "a@x.com, b@y.com"}).to == ("a@x.com", "b@y.com")


class FakeSMTP:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.logged_in: str | None = None
        self.sent: list[EmailMessage] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if self.fail:
            raise self.fail
        self.logged_in = user

    def send_message(self, msg: EmailMessage):
        self.sent.append(msg)


def test_send_uses_smtp_and_scrubs_errors(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    settings = smtp_settings()
    server = FakeSMTP()
    msg = build_message(settings, "s", "t", None)
    send(msg, settings, smtp_factory=lambda _s: server)  # type: ignore[arg-type,return-value]
    assert server.logged_in == "me@example.com" and server.sent == [msg]

    bad = FakeSMTP(fail=smtplib.SMTPAuthenticationError(535, b"bad s3cret-app-password"))
    with pytest.raises(RuntimeError) as exc:
        send(msg, settings, smtp_factory=lambda _s: bad)  # type: ignore[arg-type,return-value]
    assert "s3cret" not in str(exc.value)
    assert scrub("x s3cret-app-password y") == "x *** y"


def test_premarket_bar_notes_name_the_previous_session(
    universe_raw: pd.DataFrame, indices_raw: pd.DataFrame, cfg, calendar, tmp_path
):
    # TradingView already opened Monday's daily bar: prices must be Friday's closes,
    # and the report must say Friday -- not Monday.
    universe = retime(universe_raw, date(2026, 11, 2), date(2026, 10, 26))
    indices = retime(indices_raw, date(2026, 10, 30), date(2026, 10, 26))
    outcome = build_report(
        "monday",
        MON,
        cfg,
        FakeProvider(universe, indices),
        History(tmp_path / "h.db"),
        dry_run=True,
        calendar=calendar,
    )
    notes = " ".join(outcome.report.data_notes)
    assert "closes from the Fri 30 Oct 2026 session" in notes
    assert "volume sub-score is neutral" in notes
    first = outcome.report.buy[0]
    prev_close = universe.set_index("symbol").loc[first["symbol"], "prev_close"]
    assert first["price"] == pytest.approx(prev_close)
    assert "volume" not in first["rationale"]
