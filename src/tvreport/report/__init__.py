"""Report model and rendering (HTML email, plain text, Markdown) via Jinja2."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..overview import Overview
from ..performance import ListStats

TEMPLATES = Path(__file__).with_name("templates")
TITLES = {"monday": "Week Ahead", "friday": "Week in Review"}
DISCLAIMER = (
    "Automated rule-based technical screen of TradingView scanner data. Not financial advice. "
    "Data may be delayed (typically 15 minutes) and may contain errors; verify before acting."
)


@dataclass(frozen=True)
class PerformanceSection:
    monday_run_date: date | None
    rows: list[dict[str, Any]]
    week_stats: list[ListStats]
    cumulative_stats: list[ListStats]
    benchmark_return_pct: float | None
    note: str | None


@dataclass(frozen=True)
class ReportData:
    run_type: str
    run_date: date
    generated_at: datetime  # timezone-aware
    display_timezones: list[str]
    session_notes: list[str]
    data_notes: list[str]
    overview: Overview
    buy: list[dict[str, Any]]
    sell: list[dict[str, Any]]
    hold: list[dict[str, Any]]
    universe_size: int
    portfolio: list[dict[str, Any]] | None = None
    performance: PerformanceSection | None = None
    dry_run: bool = False
    disclaimer: str = DISCLAIMER
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return TITLES[self.run_type]

    @property
    def subject(self) -> str:
        d = self.run_date
        prefix = "[DRY RUN] " if self.dry_run else ""
        return f"{prefix}{self.title} — {d:%a} {d.day} {d:%b %Y}"

    @property
    def generated_times(self) -> list[str]:
        return [
            self.generated_at.astimezone(ZoneInfo(tz)).strftime(
                f"%a %d %b %Y %H:%M {_tz_label(tz)}"
            )
            for tz in self.display_timezones
        ]


def _tz_label(tz: str) -> str:
    return {"America/New_York": "ET", "Asia/Dubai": "Dubai"}.get(tz, tz)


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def fmt_pct(value: Any, digits: int = 1) -> str:
    return "n/a" if _is_missing(value) else f"{float(value):+.{digits}f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    return "n/a" if _is_missing(value) else f"{float(value):,.{digits}f}"


def fmt_rate(value: Any) -> str:
    return "n/a" if _is_missing(value) else f"{float(value):.0f}%"


def md_cell(value: Any) -> str:
    """Text safe inside a Markdown table cell."""

    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def pct_color(value: Any) -> str:
    if _is_missing(value):
        return "#6b7280"
    return "#047857" if float(value) > 0 else "#b91c1c" if float(value) < 0 else "#374151"


def _env(autoescape: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=autoescape,  # explicit: select_autoescape() keys on the extension (.j2)
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(pct=fmt_pct, num=fmt_num, rate=fmt_rate, pct_color=pct_color, md=md_cell)
    return env


def render_html(data: ReportData) -> str:
    return _env(True).get_template("report.html.j2").render(r=data)


def render_text(data: ReportData) -> str:
    return _env(False).get_template("report.txt.j2").render(r=data)


def render_markdown(data: ReportData) -> str:
    return _env(False).get_template("report.md.j2").render(r=data)


def save_report(data: ReportData, directory: Path) -> tuple[Path, Path]:
    """Write reports/YYYY-MM-DD-{monday|friday}.html and .md; returns both paths."""

    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{data.run_date.isoformat()}-{data.run_type}"
    html_path = directory / f"{stem}.html"
    md_path = directory / f"{stem}.md"
    html_path.write_text(render_html(data), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    return html_path, md_path
