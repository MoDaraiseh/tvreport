"""docs/sample-report.html stays labelled as a sample and in sync with the templates."""

from __future__ import annotations

from scripts.make_sample_report import OUT, build_sample_html


def test_sample_report_is_labelled_and_current():
    html = build_sample_html()
    assert "SAMPLE REPORT: built from the recorded test fixtures" in html
    assert "<title>[SAMPLE] Week Ahead" in html
    assert "DRY RUN" not in html
    committed = OUT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == html, "regenerate it: python -m scripts.make_sample_report"
