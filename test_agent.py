"""Unit tests for the network-free pure functions behind the metric tools.

Per CLAUDE.md: every metric tool is a pure function with unit tests before it
becomes a tool. These cover the P&L math (compute_snapshot) and CSV parsing
(read_portfolio); the yfinance/mftool fetch wrappers are exercised manually.
Run with: uv run pytest
"""
import pytest

from agent import compute_snapshot, read_portfolio


# --- compute_snapshot: the arithmetic the LLM is never allowed to do itself ---

def test_snapshot_basic_gain_and_loss():
    holdings = [
        {"ticker": "A.NS", "quantity": 10, "buy_price": 100.0, "buy_date": "2024-01-01"},
        {"ticker": "B.NS", "quantity": 5, "buy_price": 200.0, "buy_date": "2024-01-01"},
    ]
    prices = {"A.NS": 150.0, "B.NS": 180.0}  # A up 50%, B down 10%
    snap = compute_snapshot(holdings, prices)

    a, b = snap["holdings"]
    assert a["invested"] == 1000.0 and a["current_value"] == 1500.0
    assert a["pnl_abs"] == 500.0 and a["pnl_pct"] == 50.0
    assert b["invested"] == 1000.0 and b["current_value"] == 900.0
    assert b["pnl_abs"] == -100.0 and b["pnl_pct"] == -10.0

    assert snap["total_invested"] == 2000.0
    assert snap["total_current_value"] == 2400.0
    assert snap["total_pnl_abs"] == 400.0
    assert snap["total_pnl_pct"] == 20.0
    assert snap["priced_holdings"] == 2 and snap["total_holdings"] == 2


def test_snapshot_fractional_quantity():
    holdings = [{"ticker": "MF", "quantity": 12.5, "buy_price": 40.0, "buy_date": "2024-01-01"}]
    snap = compute_snapshot(holdings, {"MF": 44.0})
    row = snap["holdings"][0]
    assert row["invested"] == 500.0 and row["current_value"] == 550.0
    assert row["pnl_abs"] == 50.0 and row["pnl_pct"] == 10.0


def test_snapshot_missing_price_excluded_from_totals():
    holdings = [
        {"ticker": "A.NS", "quantity": 10, "buy_price": 100.0, "buy_date": "2024-01-01"},
        {"ticker": "BAD.NS", "quantity": 5, "buy_price": 50.0, "buy_date": "2024-01-01"},
    ]
    snap = compute_snapshot(holdings, {"A.NS": 100.0})  # no price for BAD.NS
    assert snap["priced_holdings"] == 1 and snap["total_holdings"] == 2
    assert snap["total_invested"] == 1000.0  # BAD.NS's 250 invested is excluded
    assert any(r.get("error") for r in snap["holdings"])


def test_snapshot_empty_holdings_no_divide_by_zero():
    snap = compute_snapshot([], {})
    assert snap["total_invested"] == 0.0 and snap["total_pnl_pct"] == 0.0


# --- read_portfolio: parsing and graceful failure ---

def _write(tmp_path, text):
    p = tmp_path / "portfolio.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_read_portfolio_valid(tmp_path):
    path = _write(tmp_path, "ticker,quantity,buy_price,buy_date\n"
                            "RELIANCE.NS,10,2450,2024-03-15\n"
                            "INFY.NS,25,1420,2024-07-02\n")
    holdings, skipped = read_portfolio(path)
    assert skipped == []
    assert len(holdings) == 2
    assert holdings[0] == {"ticker": "RELIANCE.NS", "quantity": 10.0,
                           "buy_price": 2450.0, "buy_date": "2024-03-15"}


def test_read_portfolio_tolerates_bom_header(tmp_path):
    # The shipped sample CSV has a UTF-8 BOM on the first column name.
    path = _write(tmp_path, "﻿ticker,quantity,buy_price,buy_date\nTCS.NS,5,3890,2025-01-20\n")
    holdings, _ = read_portfolio(path)
    assert holdings[0]["ticker"] == "TCS.NS"


def test_read_portfolio_skips_malformed_rows(tmp_path):
    path = _write(tmp_path, "ticker,quantity,buy_price,buy_date\n"
                            "GOOD.NS,10,100,2024-01-01\n"
                            "BAD.NS,notanumber,100,2024-01-01\n"
                            ",5,50,2024-01-01\n"
                            "NEG.NS,-3,50,2024-01-01\n")
    holdings, skipped = read_portfolio(path)
    assert [h["ticker"] for h in holdings] == ["GOOD.NS"]
    assert len(skipped) == 3  # bad qty, empty ticker, negative qty


def test_read_portfolio_missing_file_raises():
    with pytest.raises(ValueError, match="not found"):
        read_portfolio("does_not_exist_12345.csv")


def test_read_portfolio_missing_column_raises(tmp_path):
    path = _write(tmp_path, "ticker,quantity\nA.NS,10\n")
    with pytest.raises(ValueError, match="missing required column"):
        read_portfolio(path)


def test_read_portfolio_all_rows_bad_raises(tmp_path):
    path = _write(tmp_path, "ticker,quantity,buy_price,buy_date\nA.NS,bad,bad,x\n")
    with pytest.raises(ValueError, match="No valid holdings"):
        read_portfolio(path)
