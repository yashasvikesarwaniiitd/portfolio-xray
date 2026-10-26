"""Hermetic tests for broker-statement ingestion. No network, no API key.

Every P&L figure below is hand-computed in the test's docstring. The engine previously
could not express a SELL at all, so these cases are the ones most likely to be wrong, and
they are checked against arithmetic done by hand rather than against the code's own output.
"""
from datetime import date

import pytest

from xray_agent.adapters.broker_import import (ImportError_, map_columns, normalise_symbol,
                                               parse_date, parse_number, parse_side,
                                               read_transactions, to_holdings)
from xray_agent.domain import transactions as txn
from xray_agent.domain.transactions import BUY, SELL, Transaction


def T(d, side, units, price, symbol="RELIANCE.NS", name="Reliance"):
    return Transaction(date=d, side=side, units=units, price=price, symbol=symbol,
                       name=name)


# --- column mapping ------------------------------------------------------------------------

def test_common_broker_headers_are_mapped():
    m, _ = map_columns(["Trade Date", "Stock Name", "Symbol", "Transaction Type",
                        "Quantity", "Trade Price", "Net Amount"])
    assert m["date"] == "Trade Date"
    assert m["symbol"] == "Symbol"
    assert m["name"] == "Stock Name"
    assert m["side"] == "Transaction Type"
    assert m["quantity"] == "Quantity"
    assert m["price"] == "Trade Price"
    assert m["amount"] == "Net Amount"


def test_a_longer_synonym_wins_over_a_shorter_one():
    """Both "Price" and "Trade Price" are synonyms; the specific one must win, or the
    executed price gets read from whatever column happened to come first."""
    m, _ = map_columns(["Date", "Qty", "Price", "Trade Price"])
    assert m["price"] == "Trade Price"


def test_decorated_headers_still_match():
    m, _ = map_columns(["Date & Time", "Buy/Sell Indicator", "Quantity (Nos.)", "Rate"])
    assert m["date"] == "Date & Time"
    assert m["side"] == "Buy/Sell Indicator"
    assert m["quantity"] == "Quantity (Nos.)"
    assert m["price"] == "Rate"


def test_unmapped_columns_are_reported_not_silently_dropped():
    _, unmapped = map_columns(["Date", "Qty", "Price", "Brokerage", "STT", "Exchange"])
    assert set(unmapped) >= {"Brokerage", "STT", "Exchange"}


# --- field parsing -------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2025-03-14", date(2025, 3, 14)),
    ("14-03-2025", date(2025, 3, 14)),
    ("14/03/2025", date(2025, 3, 14)),
    ("14-Mar-2025", date(2025, 3, 14)),
    ("2025-03-14 09:15:00", date(2025, 3, 14)),      # clock time stripped
    ("14-03-2025 09:15 AM", date(2025, 3, 14)),
])
def test_date_formats_brokers_actually_use(raw, expected):
    assert parse_date(raw) == expected


def test_unparseable_date_returns_none_rather_than_guessing():
    assert parse_date("not a date") is None
    assert parse_date("") is None


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56),
    ("Rs 1,234.56", 1234.56),
    ("(1,234.56)", -1234.56),      # accounting negative
    ("-500", -500.0),
    ("-", None),
    ("", None),
])
def test_number_formats(raw, expected):
    assert parse_number(raw) == expected


def test_side_is_read_from_words_or_from_a_signed_quantity():
    assert parse_side("BUY", 10) == BUY
    assert parse_side("Sold", 10) == SELL
    assert parse_side("Redemption", 10) == SELL
    assert parse_side("", -10) == SELL          # unsigned side, signed quantity
    assert parse_side("", 10) == BUY
    assert parse_side("", None) is None         # neither: caller must skip the row


def test_bare_nse_tickers_get_a_suffix_but_scheme_codes_do_not():
    assert normalise_symbol("RELIANCE") == ("RELIANCE.NS", "yfinance")
    assert normalise_symbol("INFY.NS") == ("INFY.NS", "yfinance")
    assert normalise_symbol("122639") == ("122639", "mftool")
    assert normalise_symbol("BTC-USD") == ("BTC-USD", "crypto")


# --- whole-file parsing ----------------------------------------------------------------------

STATEMENT = """Groww Order Statement
Account: XXXXX

Trade Date,Stock Name,Symbol,Transaction Type,Quantity,Trade Price,Net Amount
2025-03-14,Reliance Industries,RELIANCE,BUY,10,1200.00,12000.00
2025-04-14,Reliance Industries,RELIANCE,BUY,10,1400.00,14000.00
2025-09-10,Reliance Industries,RELIANCE,SELL,5,1600.00,8000.00
2025-05-02,Infosys,INFY,BUY,20,1500.00,30000.00
"""


def test_banner_lines_before_the_header_are_skipped():
    """Broker exports prepend account banners. The header is found by looking for a row
    that maps to a date plus a quantity, not by assuming line 1."""
    txns, skipped, mapping, _ = read_transactions(STATEMENT)
    assert len(txns) == 4
    assert skipped == []
    assert mapping["date"] == "Trade Date"


def test_a_file_with_no_recognisable_header_says_what_it_saw():
    with pytest.raises(ImportError_) as e:
        read_transactions("Alpha,Beta,Gamma\n1,2,3\n")
    assert "Columns I saw" in str(e.value)


def test_one_bad_row_does_not_cost_the_whole_import():
    bad = STATEMENT.replace("2025-05-02,Infosys,INFY,BUY,20,1500.00,30000.00",
                            "oops,Infosys,INFY,BUY,20,1500.00,30000.00")
    txns, skipped, _, _ = read_transactions(bad)
    assert len(txns) == 3
    assert len(skipped) == 1 and "unreadable date" in skipped[0]


def test_price_is_derived_from_amount_when_absent():
    csv_text = ("Date,Symbol,Type,Quantity,Amount\n"
                "2025-03-14,RELIANCE,BUY,10,12000\n")
    txns, _, _, _ = read_transactions(csv_text)
    assert txns[0].price == 1200.0


def test_transactions_group_into_holdings_largest_first():
    txns, _, _, _ = read_transactions(STATEMENT)
    holdings = to_holdings(txns)
    assert [h["name"] for h in holdings] == ["Infosys", "Reliance Industries"]
    assert holdings[0]["symbol"] == "INFY.NS"
    assert holdings[0]["source"] == "yfinance"
    # gross buys, not net of the sale
    assert holdings[1]["total_invested"] == 26000.00
    assert len(holdings[1]["transactions"]) == 3


# --- the maths that the old engine could not do at all -----------------------------------------

def test_units_at_tracks_a_position_down_through_a_sell():
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 4, 14), BUY, 10, 1400.0),
              T(date(2025, 9, 10), SELL, 5, 1600.0)]
    assert txn.units_at(trades, date(2025, 3, 31)) == 10
    assert txn.units_at(trades, date(2025, 8, 1)) == 20
    assert txn.units_at(trades, date(2025, 12, 1)) == 15


def test_realised_pnl_is_fifo_matched():
    """Bought 10 @ 1200 then 10 @ 1400; sold 5 @ 1600.
    FIFO takes the 1200 lot first: 5 x (1600 - 1200) = +2,000 realised.
    Remaining: 5 @ 1200 + 10 @ 1400 = 20,000 cost over 15 units = 1333.3333 average."""
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 4, 14), BUY, 10, 1400.0),
              T(date(2025, 9, 10), SELL, 5, 1600.0)]
    r = txn.realised_pnl(trades)
    assert r["realised_pnl"] == 2000.00
    assert r["open_units"] == 15
    assert r["open_cost"] == 20000.00
    assert r["average_cost"] == 1333.3333


def test_a_sell_spanning_two_lots_matches_both():
    """Sell 12 against lots of 10 @ 1200 and 10 @ 1400, at 1600:
    10 x 400 = 4,000, then 2 x 200 = 400  ->  4,400 realised."""
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 4, 14), BUY, 10, 1400.0),
              T(date(2025, 9, 10), SELL, 12, 1600.0)]
    r = txn.realised_pnl(trades)
    assert r["realised_pnl"] == 4400.00
    assert r["open_units"] == 8
    assert len(r["closed_lots"]) == 2


def test_selling_more_than_was_bought_is_reported_not_guessed():
    """A statement that starts mid-history has sells with no matching buy. Those units
    must be surfaced, never silently assigned a cost basis of zero — that would invent a
    profit equal to the entire sale proceeds."""
    trades = [T(date(2025, 3, 14), BUY, 5, 1200.0),
              T(date(2025, 9, 10), SELL, 8, 1600.0)]
    r = txn.realised_pnl(trades)
    assert r["unmatched_units"] == 3
    assert r["realised_pnl"] == 2000.00          # only the 5 matched units
    assert "never guessed" in r["unmatched_note"]


def test_same_day_buy_is_ordered_before_a_same_day_sell():
    """Otherwise a same-day round trip looks like selling stock not yet held."""
    trades = [T(date(2025, 3, 14), SELL, 5, 1300.0),
              T(date(2025, 3, 14), BUY, 10, 1200.0)]
    r = txn.realised_pnl(trades)
    assert r["realised_pnl"] == 500.00           # 5 x (1300 - 1200)
    assert "unmatched_units" not in r


def test_sale_proceeds_are_a_positive_cashflow_for_xirr():
    """The classic XIRR bug is signing a sale the same way as a purchase, which turns a
    profitable exit into a loss. Money out is negative, money back is positive."""
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 9, 10), SELL, 5, 1600.0)]
    flows = txn.cashflows_for_xirr(trades, date(2026, 3, 14), 1500.0)
    assert flows[0] == (date(2025, 3, 14), -12000.0)
    assert flows[1] == (date(2025, 9, 10), 8000.0)
    assert flows[2] == (date(2026, 3, 14), 7500.0)      # 5 units still held
    import metrics
    assert metrics.xirr(flows) > 0                       # bought 1200, worth 1500: a gain


def test_priced_flows_let_the_existing_engine_handle_sells_unchanged():
    """The whole integration trick: a sell becomes a negative amount at a positive price,
    so metrics.units_held_at (amount / price) returns negative units and attribute_move
    needs no modification at all."""
    import metrics
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 9, 10), SELL, 4, 1600.0)]
    flows = txn.to_priced_flows(trades)
    assert metrics.units_held_at(flows, date(2025, 3, 31)) == 10
    assert metrics.units_held_at(flows, date(2025, 12, 1)) == 6


def test_summarise_separates_realised_from_unrealised():
    """15 units open at 20,000 cost, valued at 1500 = 22,500 -> +2,500 unrealised.
    Plus 2,000 already realised on the sale -> 4,500 total."""
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 4, 14), BUY, 10, 1400.0),
              T(date(2025, 9, 10), SELL, 5, 1600.0)]
    s = txn.summarise(trades, date(2026, 3, 14), 1500.0)
    assert s["units"] == 15
    assert s["current_value"] == 22500.00
    assert s["unrealised_pnl"] == 2500.00
    assert s["realised_pnl"] == 2000.00
    assert s["total_pnl"] == 4500.00
    assert s["gross_invested"] == 26000.00
    assert s["gross_sold"] == 8000.00
    assert s["net_invested"] == 18000.00


def test_a_fully_exited_position_reports_its_realised_result():
    trades = [T(date(2025, 3, 14), BUY, 10, 1200.0),
              T(date(2025, 9, 10), SELL, 10, 1600.0)]
    s = txn.summarise(trades, date(2026, 3, 14), 1500.0)
    assert s["status"] == "fully exited"
    assert s["total_pnl"] == 4000.00


# --- the file-path route into the engine -------------------------------------------------

def _write(tmp_path, text, name="statement.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_a_statement_with_banner_lines_is_detected_through_read_portfolio(tmp_path):
    """Regression. Banner lines make the file non-rectangular, so pandas raised BEFORE
    the missing-column check that triggered the broker fallback — the fallback was never
    consulted and a real export just failed. Unit tests missed it because they feed
    read_transactions a tidy string; only running an actual export found it."""
    import agent
    holdings, notes = agent.read_portfolio(_write(tmp_path, STATEMENT))
    assert len(holdings) == 2
    assert "broker order statement" in notes[0]
    assert "4 transactions" in notes[0]


def test_a_clean_statement_with_no_banner_also_works(tmp_path):
    import agent
    clean = STATEMENT.split("\n", 3)[3]
    holdings, notes = agent.read_portfolio(_write(tmp_path, clean))
    assert len(holdings) == 2


def test_the_sip_sheet_still_takes_the_old_path(tmp_path):
    """The new fallback must not change how the original format is read."""
    import agent
    sip = ("Mode,App,Type,Market,Risk,Where,symbol,source,Total Invested,Mar'25\n"
           "Equity,Groww,Share,India,Med,Reliance,RELIANCE.NS,yfinance,5000,5000\n")
    holdings, notes = agent.read_portfolio(_write(tmp_path, sip))
    assert len(holdings) == 1
    assert "transactions" not in holdings[0]      # reconstruction path, not statement path
    assert holdings[0]["inflows"] == [(2025, 3, 5000.0)]


def test_a_file_that_is_neither_format_names_both_failures(tmp_path):
    import agent
    with pytest.raises(ValueError) as e:
        agent.read_portfolio(_write(tmp_path, "Alpha,Beta\n1,2\n"))
    msg = str(e.value)
    assert "missing required column" in msg
    assert "broker order statement" in msg
