"""The Health report tab shows a report's findings by reading them back out of the workbook.

These tests build the summary sheet with the REAL builder functions rather than a hand-made
imitation, so if the builder's layout drifts the preview fails here instead of silently
showing an empty tab. Hermetic: no network, no AI.
"""
import io

import pytest
from openpyxl import Workbook

import report_view as V
from report import builder


def workbook_bytes(bullets, score_value=35.3, with_sections=True):
    wb = Workbook()
    bundle = {"holdings_count": 6, "level": "L2",
              "sections": {"diversification": {"effective_holdings": 4.8,
                                               "diversification_score": score_value},
                           "risk": {"portfolio_beta_est": 1.1, "high_risk_tier_pct": 52.5}}}
    builder._exec_summary(wb, bundle, {}, bullets)
    builder._how_to_use(wb, "L2")
    if with_sections:
        for title in ("1 · Asset Allocation", "2 · Diversification"):
            builder.banner(builder.sheet(wb, title), title, "x")
    builder._methodology(wb, bundle)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_the_executive_summary_findings_are_read_back_without_their_bullet():
    data = workbook_bytes(["Two funds own most of the same stocks.",
                           "Question: is one of them doing the work of two?"])
    assert V.findings(data) == ["Two funds own most of the same stocks.",
                                "Question: is one of them doing the work of two?"]


def test_the_stated_diversification_score_is_read_back():
    assert V.score(workbook_bytes(["x"], score_value=35.3)) == pytest.approx(35.3)


def test_only_the_analysis_sheets_are_listed_not_the_summary_or_the_explainers():
    names = V.sections(workbook_bytes(["x"]))
    assert names == ["1 · Asset Allocation", "2 · Diversification"]


def test_a_report_with_no_findings_gives_an_empty_list_not_an_error():
    assert V.findings(workbook_bytes([])) == []


def test_findings_are_capped_so_a_long_summary_cannot_flood_the_page():
    assert len(V.findings(workbook_bytes([f"finding {i}" for i in range(30)]), limit=4)) == 4


def test_anything_that_is_not_a_workbook_degrades_to_nothing_rather_than_raising():
    """A preview must never stop someone downloading the report it previews."""
    for junk in (b"", b"not a zip at all", b"PK\x03\x04 corrupt"):
        assert V.findings(junk) == [] and V.sections(junk) == [] and V.score(junk) is None


def test_a_workbook_with_no_summary_sheet_degrades_to_nothing():
    wb = Workbook()
    wb.active.title = "Something else"
    buf = io.BytesIO()
    wb.save(buf)
    assert V.findings(buf.getvalue()) == [] and V.score(buf.getvalue()) is None
