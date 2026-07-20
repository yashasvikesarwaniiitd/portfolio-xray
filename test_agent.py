"""Tests for CSV parsing, snapshot aggregation, and the MANDATORY live regression.

The Parag Parikh regression reconstructs units from live AMFI NAVs via mftool and must
reproduce the hand-validated ground truth (units ~1164.541, value ~Rs 106,509, XIRR ~0.69%).
It needs network access. Pure metric math is covered in test_metrics.py.
Run with: uv run pytest
"""
import pytest

from agent import aggregate_snapshot, parse_month_label, read_portfolio, reconstruct_holding

REAL_HEADER = ("Mode,App,Type,Market,Risk,Where,symbol,source,"
               '"Estimated Returns (3Y)",Total Invested,'
               "Mar'25,April'25,May'25,June'25,July'25\n")

# The exact Parag Parikh holding as read_portfolio would produce it (Direct Growth, 122639).
PP_HOLDING = {
    "name": "Parag Parikh Flexi Cap", "symbol": "122639", "source": "mftool",
    "type": "Mutual Fund", "market": "India", "risk": "Med", "total_invested": 106000,
    "inflows": [(2025, 3, 2000), (2025, 4, 2000), (2025, 5, 2000), (2025, 6, 10000),
                (2025, 7, 10000), (2025, 8, 5000), (2025, 9, 12000), (2025, 10, 10000),
                (2025, 11, 4000), (2025, 12, 9000), (2026, 1, 9000), (2026, 3, 7000),
                (2026, 4, 12000), (2026, 6, 12000)],
}


# === MANDATORY regression: locks the return engine to a hand-verified number ===

@pytest.mark.regression
def test_parag_parikh_live_reconstruction():
    r = reconstruct_holding(PP_HOLDING)
    assert r["status"] == "priced", r
    assert r["priced_sips"] == 14  # every SIP, incl. Saturday-dated Mar'25, must price
    assert r["units"] == pytest.approx(1164.541, rel=0.005)       # ~0.5% NAV-date tolerance
    assert r["current_value"] == pytest.approx(106509, rel=0.005)
    assert r["invested"] == pytest.approx(106000, rel=0.005)
    assert r["xirr_pct"] == pytest.approx(0.69, abs=0.1)


# === snapshot aggregation (network-free) ===

def test_aggregate_excludes_unavailable_from_totals():
    rows = [
        {"status": "priced", "invested": 1000.0, "current_value": 1500.0},
        {"status": "priced", "invested": 1000.0, "current_value": 900.0},
        {"status": "unavailable", "invested": 500.0, "reason": "manual"},
    ]
    snap = aggregate_snapshot(rows)
    assert snap["priced_count"] == 2 and snap["total_count"] == 3
    assert snap["total_invested"] == 2000.0
    assert snap["total_current_value"] == 2400.0
    assert snap["total_pnl_abs"] == 400.0
    assert snap["total_pnl_pct"] == 20.0
    assert snap["unpriced_invested"] == 500.0


def test_aggregate_all_unavailable_no_divide_by_zero():
    snap = aggregate_snapshot([{"status": "unavailable", "invested": 500.0}])
    assert snap["priced_count"] == 0
    assert snap["total_pnl_pct"] == 0.0
    assert snap["unpriced_invested"] == 500.0


# === month-label parsing ===

@pytest.mark.parametrize("label,expected", [
    ("Mar'25", (2025, 3)), ("April'25", (2025, 4)), ("June'25", (2025, 6)),
    ("July'25", (2025, 7)), ("Dec'25", (2025, 12)), ("Jan'26", (2026, 1)),
    ("Feb'27", (2027, 2)),
])
def test_parse_month_label(label, expected):
    assert parse_month_label(label) == expected


def test_parse_month_label_bad_raises():
    with pytest.raises(ValueError):
        parse_month_label("notamonth'25")


# === read_portfolio: real multi-source schema ===

def _write(tmp_path, text):
    p = tmp_path / "portfolio.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_read_portfolio_multisource(tmp_path):
    path = _write(tmp_path, REAL_HEADER +
        'Equity,Groww,Share,India,Med,Ashoka,ASHOKA.NS,yfinance,-,14703,-,-,-,1653,5495\n'
        'Equity,Groww,Mutual Fund,Global,High,Axis FoF,148485,mftool,-,11000,-,-,-,1000,1000\n'
        'Crypto,Groww,Crypto,Global,High,Bitcoin,BTC-USD,crypto,-,5000,-,-,-,5000,-\n'
        'Equity,X,Basket,India,Med,Smallcase,,manual,-,6113,-,-,-,-,-\n')
    holdings, skipped = read_portfolio(path)
    assert skipped == [] and len(holdings) == 4
    by_name = {h["name"]: h for h in holdings}
    fund = by_name["Axis FoF"]
    assert fund["source"] == "mftool" and fund["symbol"] == "148485"
    assert fund["inflows"] == [(2025, 6, 1000.0), (2025, 7, 1000.0)]
    manual = by_name["Smallcase"]
    assert manual["source"] == "manual" and manual["symbol"] is None and manual["inflows"] == []


def test_read_portfolio_skips_nameless_footer_and_spacers(tmp_path):
    # Blank spacer + a summary-footer row (blank Where) must be silently ignored.
    path = _write(tmp_path, REAL_HEADER +
        'Equity,Groww,Share,India,Med,Ashoka,ASHOKA.NS,yfinance,-,14703,-,-,-,1653,5495\n'
        ',,,,,,,,,,,,,,\n'
        ',,,,,,,,,Overall Investments,636843,,,,\n')
    holdings, skipped = read_portfolio(path)
    assert len(holdings) == 1 and skipped == []


def test_read_portfolio_bom_header(tmp_path):
    path = _write(tmp_path, "﻿" + REAL_HEADER +
        'Equity,Groww,Share,India,Med,Ashoka,ASHOKA.NS,yfinance,-,14703,-,-,-,-,-\n')
    holdings, _ = read_portfolio(path)
    assert holdings[0]["name"] == "Ashoka" and holdings[0]["inflows"] == []


def test_read_portfolio_missing_file_raises():
    with pytest.raises(ValueError, match="not found"):
        read_portfolio("does_not_exist_12345.csv")


def test_read_portfolio_missing_column_raises(tmp_path):
    path = _write(tmp_path, "Where,symbol\nAshoka,ASHOKA.NS\n")
    with pytest.raises(ValueError, match="missing required column"):
        read_portfolio(path)
