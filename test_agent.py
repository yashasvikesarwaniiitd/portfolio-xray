"""Unit tests for the network-free pure functions behind the metric tools.

Per CLAUDE.md: every metric tool is a pure function with unit tests before it becomes a
tool. These cover the valuation math (value_from_avg_cost), portfolio aggregation
(aggregate_snapshot), month-label parsing, and CSV parsing (read_portfolio) against the
real portfolio schema. The yfinance/mftool fetch wrappers are exercised manually.
Run with: uv run pytest
"""
import pytest

from agent import (
    aggregate_snapshot,
    parse_month_label,
    read_portfolio,
    value_from_avg_cost,
)

REAL_HEADER = ("Mode,App,Type,Market,Risk,Where,symbol,source,"
               '"Estimated Returns (3Y)",Total Invested,'
               "Mar'25,April'25,May'25,June'25,July'25\n")


# --- value_from_avg_cost: the estimate arithmetic the LLM must never do itself ---

def test_value_basic_gain():
    # invested 1000 at avg price 100 -> 10 units; now 150 -> value 1500, +50%
    v = value_from_avg_cost(1000.0, 100.0, 150.0)
    assert v["units_est"] == 10.0
    assert v["current_value_est"] == 1500.0
    assert v["pnl_abs_est"] == 500.0
    assert v["pnl_pct_est"] == 50.0


def test_value_loss():
    v = value_from_avg_cost(1000.0, 100.0, 80.0)
    assert v["current_value_est"] == 800.0
    assert v["pnl_abs_est"] == -200.0
    assert v["pnl_pct_est"] == -20.0


def test_value_zero_invested_no_divide_by_zero():
    v = value_from_avg_cost(0.0, 100.0, 150.0)
    assert v["pnl_pct_est"] == 0.0


# --- aggregate_snapshot: totals include only priced holdings ---

def test_aggregate_excludes_unavailable_from_totals():
    rows = [
        {"status": "priced", "invested": 1000.0, "current_value_est": 1500.0},
        {"status": "priced", "invested": 1000.0, "current_value_est": 900.0},
        {"status": "unavailable", "invested": 500.0, "reason": "manual"},
    ]
    snap = aggregate_snapshot(rows)
    assert snap["priced_count"] == 2 and snap["total_count"] == 3
    assert snap["total_invested_priced"] == 2000.0
    assert snap["total_current_value_est"] == 2400.0
    assert snap["total_pnl_abs_est"] == 400.0
    assert snap["total_pnl_pct_est"] == 20.0
    assert snap["unpriced_invested"] == 500.0  # the manual holding's capital, kept visible


def test_aggregate_all_unavailable_no_divide_by_zero():
    rows = [{"status": "unavailable", "invested": 500.0, "reason": "x"}]
    snap = aggregate_snapshot(rows)
    assert snap["priced_count"] == 0
    assert snap["total_pnl_pct_est"] == 0.0
    assert snap["unpriced_invested"] == 500.0


# --- parse_month_label: inconsistent 3-letter / full month names ---

@pytest.mark.parametrize("label,expected", [
    ("Mar'25", (2025, 3)),
    ("April'25", (2025, 4)),
    ("June'25", (2025, 6)),
    ("July'25", (2025, 7)),
    ("Dec'25", (2025, 12)),
    ("Jan'26", (2026, 1)),
    ("Feb'27", (2027, 2)),
])
def test_parse_month_label(label, expected):
    assert parse_month_label(label) == expected


def test_parse_month_label_bad_raises():
    with pytest.raises(ValueError):
        parse_month_label("notamonth'25")


# --- read_portfolio: real multi-source schema ---

def _write(tmp_path, text):
    p = tmp_path / "portfolio.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_read_portfolio_multisource(tmp_path):
    path = _write(tmp_path, REAL_HEADER +
        'Equity,Groww,Share,India,Med,Ashoka,ASHOKABLDCON.NS,yfinance,-,14703,-,-,-,1653,5495\n'
        'Equity,Groww,Mutual Fund,Global,High,Axis FoF,148485,mftool,-,11000,-,-,-,1000,1000\n'
        'Crypto,Groww,Crypto,Global,High,Bitcoin,BTC-USD,crypto,-,5000,-,-,-,5000,-\n'
        'Equity,X,Basket,India,Med,Smallcase,,manual,-,6113,-,-,-,-,-\n')
    holdings, skipped = read_portfolio(path)
    assert skipped == []
    assert len(holdings) == 4
    by_name = {h["name"]: h for h in holdings}

    fund = by_name["Axis FoF"]
    assert fund["source"] == "mftool" and fund["symbol"] == "148485"
    assert fund["total_invested"] == 11000.0
    assert fund["inflows"] == [(2025, 6, 1000.0), (2025, 7, 1000.0)]

    manual = by_name["Smallcase"]
    assert manual["source"] == "manual" and manual["symbol"] is None
    assert manual["inflows"] == []  # all months blank/'-'


def test_read_portfolio_blank_source_preserved(tmp_path):
    # 5th, undocumented source case: blank source must parse (not crash) and stay blank.
    path = _write(tmp_path, REAL_HEADER +
        'Equity,X,Share,India,Med,Mystery,SOMETHING.NS,,-,2000,-,-,-,2000,-\n')
    holdings, _ = read_portfolio(path)
    assert holdings[0]["source"] == ""
    assert holdings[0]["symbol"] == "SOMETHING.NS"


def test_read_portfolio_bom_and_dashes(tmp_path):
    # BOM on first header cell; '-' inflow cells count as zero.
    path = _write(tmp_path, "﻿" + REAL_HEADER +
        'Equity,Groww,Share,India,Med,Ashoka,ASHOKABLDCON.NS,yfinance,-,14703,-,-,-,-,-\n')
    holdings, _ = read_portfolio(path)
    assert holdings[0]["name"] == "Ashoka"
    assert holdings[0]["inflows"] == []


def test_read_portfolio_missing_file_raises():
    with pytest.raises(ValueError, match="not found"):
        read_portfolio("does_not_exist_12345.csv")


def test_read_portfolio_missing_column_raises(tmp_path):
    path = _write(tmp_path, "Where,symbol\nAshoka,ASHOKABLDCON.NS\n")
    with pytest.raises(ValueError, match="missing required column"):
        read_portfolio(path)
