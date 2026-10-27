"""Tests for merging an upload into an existing portfolio.

The rule being protected is that "add to my portfolio" never double-counts. Each test
states the failure it would otherwise let through - almost all of them are silent, because
a doubled trade produces a plausible-looking portfolio that is simply twice as large.
Hermetic: no network (funds are read with resolve_funds=False).
"""
import pytest

import agent
import portfolio_merge as M

STMT_HEAD = "Date,Symbol,Stock name,Type,Quantity,Price,Order status\n"
STMT_A = (STMT_HEAD +
          "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n"
          "2026-02-10,HDFCBANK,HDFC Bank,BUY,5,1600,Executed\n")
# Overlaps A by the HDFC trade, and adds two genuinely new ones.
STMT_B = (STMT_HEAD +
          "2026-02-10,HDFCBANK,HDFC Bank,BUY,5,1600,Executed\n"
          "2026-03-02,TCS,TCS,BUY,2,3800,Executed\n"
          "2026-04-01,INFY,Infosys,BUY,4,1550,Executed\n")

MF_STMT = ("Scheme Name,Transaction Type,Units,NAV,Date,Amount\n"
           "Parag Parikh Flexi Cap Fund - Direct Growth,Purchase,10,70,2026-01-15,700\n")

SIP_HEAD = "Mode,App,Type,Market,Risk,Where,symbol,source,Estimated Returns (3Y),Total Invested"
SIP_A = (SIP_HEAD + ",Mar'25,April'25\n"
         "Equity,Groww,Share,India,Med,Reliance,RELIANCE.NS,yfinance,-,10000,5000,5000\n")
SIP_B = (SIP_HEAD + ",Apr'25,May'25\n"
         "Equity,Groww,Share,India,Med,Reliance,RELIANCE.NS,yfinance,-,10000,5000,5000\n"
         "Equity,Groww,Share,India,Med,Infosys,INFY.NS,yfinance,-,6000,3000,3000\n")


def holdings_of(text, tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(text, encoding="utf-8")
    return agent.read_portfolio(str(p), resolve_funds=False)[0]


def units(h):
    return sum(t.signed_units for t in h["transactions"])


def by_name(text, tmp_path):
    return {h["name"]: h for h in holdings_of(text, tmp_path)}


# --- the unique key --------------------------------------------------------------------------

def test_a_row_key_ignores_column_order_and_number_formatting():
    """Two exports of one trade must be recognised as one trade even when the broker
    reordered the columns or wrote 1,600.00 where it used to write 1600."""
    a = M.row_key(["Date", "Symbol", "Quantity", "Price"], ["2026-01-05", "INFY", "10", "1600"])
    b = M.row_key(["Price", "Quantity", "Symbol", "Date"], ["1,600.00", "10", " infy ", "2026-01-05"])
    assert a == b


def test_a_row_key_ignores_a_blank_column_but_not_a_changed_value():
    base = M.row_key(["Date", "Quantity"], ["2026-01-05", "10"])
    assert M.row_key(["Date", "Quantity", "Notes"], ["2026-01-05", "10", ""]) == base
    assert M.row_key(["Date", "Quantity"], ["2026-01-05", "11"]) != base


# --- statements ------------------------------------------------------------------------------

def test_uploading_the_same_statement_twice_changes_nothing(tmp_path):
    """The headline failure: a re-upload silently doubling every position."""
    once, _ = M.merge("", STMT_A)
    twice, rep = M.merge(once, STMT_A)
    assert rep["changed"] is False
    assert rep["transactions_added"] == 0 and rep["transactions_duplicate"] == 2
    assert {n: units(h) for n, h in by_name(twice, tmp_path).items()} == {
        n: units(h) for n, h in by_name(once, tmp_path).items()}


def test_overlapping_exports_add_only_the_new_trades(tmp_path):
    """Exporting 'last quarter' then 'last two months' overlaps. The shared trade must be
    counted once, and the two new ones must arrive."""
    merged, rep = M.merge(STMT_A, STMT_B)
    assert rep["transactions_added"] == 2 and rep["transactions_duplicate"] == 1
    got = {n: units(h) for n, h in by_name(merged, tmp_path).items()}
    assert got == {"Infosys": 14.0, "HDFC Bank": 5.0, "TCS": 2.0}


def test_two_identical_fills_in_one_file_are_two_trades(tmp_path):
    """A set-based dedupe would collapse these and lose real shares. The multiset rule
    keeps both while still ignoring a re-upload."""
    twin = STMT_HEAD + ("2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n" * 2)
    merged, _ = M.merge("", twin)
    assert units(by_name(merged, tmp_path)["Infosys"]) == 20.0
    again, rep = M.merge(merged, twin)
    assert rep["changed"] is False
    assert units(by_name(again, tmp_path)["Infosys"]) == 20.0


def test_a_reordered_export_of_the_same_statement_still_dedupes():
    reordered = ("Order status,Price,Quantity,Type,Stock name,Symbol,Date\n"
                 "Executed,1500,10,BUY,Infosys,INFY,2026-01-05\n")
    _, rep = M.merge(STMT_A, reordered)
    assert rep["transactions_added"] == 0 and rep["transactions_duplicate"] == 1


def test_a_statement_with_banner_lines_still_merges(tmp_path):
    banner = "Groww Order Statement\nAccount: XYZ\n\n" + STMT_A
    merged, _ = M.merge("", banner)
    again, rep = M.merge(merged, STMT_B)
    assert rep["transactions_added"] == 2
    assert set(by_name(again, tmp_path)) == {"Infosys", "HDFC Bank", "TCS"}


# --- stocks AND mutual funds in one portfolio ------------------------------------------------

def test_a_stock_statement_and_a_fund_statement_make_one_portfolio(tmp_path):
    """The case the feature exists for: different columns, so different sections, one
    portfolio. Previously the second upload could only replace the first."""
    merged, rep = M.merge(STMT_A, MF_STMT)
    assert rep["sections_added"] == 1
    assert len(M.split_parts(merged)) == 2
    names = set(by_name(merged, tmp_path))
    assert {"Infosys", "HDFC Bank", "Parag Parikh Flexi Cap Fund - Direct Growth"} <= names


def test_the_same_stock_in_two_ledger_sections_is_one_holding(tmp_path):
    other_cols = ("Trade date,Scrip,Company name,Buy/Sell,Qty,Rate\n"
                  "2026-05-01,INFY,Infosys,BUY,3,1600\n")
    merged, rep = M.merge(STMT_A, other_cols)
    assert rep["sections_added"] == 1
    hs = [h for h in holdings_of(merged, tmp_path) if h["name"] == "Infosys"]
    assert len(hs) == 1 and units(hs[0]) == 13.0


def test_a_holding_in_both_a_ledger_and_a_sip_sheet_is_flagged(tmp_path):
    """Merging these would drop the SIP history (the engine prefers real transactions);
    keeping both double-counts. Neither is silent: the user is told."""
    sip = (SIP_HEAD + ",Mar'25\n"
           "Equity,Groww,Share,India,Med,Infosys,INFY.NS,yfinance,-,3000,3000\n")
    merged, _ = M.merge(STMT_A, sip)
    p = tmp_path / "m.csv"
    p.write_text(merged, encoding="utf-8")
    holdings, notes = agent.read_portfolio(str(p), resolve_funds=False)
    assert sum(1 for h in holdings if h["name"] == "Infosys") == 2
    assert any("counted as two holdings" in n for n in notes)


# --- SIP sheets ------------------------------------------------------------------------------

def test_a_new_month_extends_the_existing_holding_and_new_holdings_are_added(tmp_path):
    merged, rep = M.merge(SIP_A, SIP_B)
    assert rep["holdings_added"] == 1          # Infosys
    assert rep["months_added"] == 1            # Reliance May'25
    assert rep["months_unchanged"] == 1        # Reliance Apr'25 == A's April'25
    got = by_name(merged, tmp_path)
    assert set(got) == {"Reliance", "Infosys"}
    assert sorted(a for _, _, a in got["Reliance"]["inflows"]) == [5000.0, 5000.0, 5000.0]
    assert got["Reliance"]["total_invested"] == 15000.0
    assert got["Infosys"]["total_invested"] == 6000.0


def test_april_and_apr_are_the_same_month():
    """The real sheet mixes "Apr'25" and "April'25". Treating them as different columns
    would count one month's SIP twice."""
    merged, _ = M.merge(SIP_A, SIP_B)
    months = [c for c in merged.splitlines()[0].split(",") if "'" in c]
    assert len(months) == 3, months            # Mar, Apr (once), May


def test_a_corrected_month_replaces_the_old_figure_and_adjusts_the_total(tmp_path):
    fixed = (SIP_HEAD + ",Mar'25\n"
             "Equity,Groww,Share,India,Med,Reliance,RELIANCE.NS,yfinance,-,0,7000\n")
    merged, rep = M.merge(SIP_A, fixed)
    assert rep["months_replaced"] == 1
    r = by_name(merged, tmp_path)["Reliance"]
    assert sorted(a for _, _, a in r["inflows"]) == [5000.0, 7000.0]
    assert r["total_invested"] == 12000.0, "10000 + (7000 - 5000)"


def test_merging_the_same_sheet_twice_is_a_no_op(tmp_path):
    once, _ = M.merge("", SIP_A)
    twice, rep = M.merge(once, SIP_A)
    assert rep["changed"] is False
    assert holdings_of(twice, tmp_path) == holdings_of(once, tmp_path)


def test_sheet_summary_rows_are_not_merged_in_as_holdings(tmp_path):
    # Real sheets label the footer in another column and leave Where blank.
    footer = SIP_A + ",,,,,,,,,,,\nOverall Investments,,,,,,,,,10000,,\n"
    merged, _ = M.merge(SIP_A, footer)
    assert set(by_name(merged, tmp_path)) == {"Reliance"}


# --- guards ----------------------------------------------------------------------------------

def test_a_file_that_is_neither_format_is_refused_with_a_reason():
    with pytest.raises(ValueError, match="does not look like"):
        M.merge(STMT_A, "name,colour\nbob,blue\n")


def test_an_empty_upload_is_refused():
    with pytest.raises(ValueError):
        M.merge(STMT_A, "   \n")


def test_a_file_with_no_delimiter_is_one_part_and_reads_as_before(tmp_path):
    """Portfolios saved before merging existed carry no delimiter and must not change."""
    assert len(M.split_parts(STMT_A)) == 1
    assert set(by_name(STMT_A, tmp_path)) == {"Infosys", "HDFC Bank"}


def test_split_and_join_round_trip():
    parts = ["a,b\n1,2", "c,d\n3,4"]
    assert M.split_parts(M.join_parts(parts)) == parts


def test_describe_says_what_happened_in_plain_words():
    _, same = M.merge(STMT_A, STMT_A)
    assert M.describe(same).startswith("Nothing new")
    _, more = M.merge(STMT_A, STMT_B)
    d = M.describe(more)
    assert "2 new transaction(s) added" in d and "1 already present" in d
