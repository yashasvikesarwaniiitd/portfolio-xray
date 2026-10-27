"""Hermetic tests for broker-statement ingestion. No network, no API key.

Every P&L figure below is hand-computed in the test's docstring. The engine previously
could not express a SELL at all, so these cases are the ones most likely to be wrong, and
they are checked against arithmetic done by hand rather than against the code's own output.
"""
import json
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
    """Exchange IS mapped now (it picks the .NS/.BO suffix), so the leftovers are the
    genuinely unused ones. A user should be able to see what was ignored."""
    _, unmapped = map_columns(["Date", "Qty", "Price", "Brokerage", "STT", "Exchange"])
    assert set(unmapped) >= {"Brokerage", "STT"}
    assert "Exchange" not in unmapped


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
    txns, skipped, mapping, _, _ = read_transactions(STATEMENT)
    assert len(txns) == 4
    assert skipped == []
    assert mapping["date"] == "Trade Date"


def test_a_file_with_no_recognisable_header_says_what_it_saw():
    with pytest.raises(ImportError_) as e:
        read_transactions("Alpha,Beta,Gamma\n1,2,3\n")
    msg = str(e.value)
    assert "does not look like a transaction report" in msg
    # names the columns it DID find — the message is read by someone holding a file
    # they did not design, so refusing without saying what was seen is useless
    assert "Alpha, Beta, Gamma" in msg


def test_one_bad_row_does_not_cost_the_whole_import():
    bad = STATEMENT.replace("2025-05-02,Infosys,INFY,BUY,20,1500.00,30000.00",
                            "oops,Infosys,INFY,BUY,20,1500.00,30000.00")
    txns, skipped, _, _, _ = read_transactions(bad)
    assert len(txns) == 3
    assert len(skipped) == 1 and "unreadable date" in skipped[0]


def test_price_is_derived_from_amount_when_absent():
    csv_text = ("Date,Symbol,Type,Quantity,Amount\n"
                "2025-03-14,RELIANCE,BUY,10,12000\n")
    txns, _, _, _, _ = read_transactions(csv_text)
    assert txns[0].price == 1200.0


def test_transactions_group_into_holdings_largest_first():
    txns, _, _, _, _ = read_transactions(STATEMENT)
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


# --- the real Groww export: order status and exchange ------------------------------------

GROWW_HEADER = ["Stock name", "Symbol", "ISIN", "Type", "Quantity", "Value", "Exchange",
                "Exchange Order Id", "Execution date and time", "Order status"]


def _groww_tsv(rows):
    return "\t".join(GROWW_HEADER) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n"


def test_the_real_groww_header_maps_and_derives_price_from_value():
    """The actual export has no price column — price comes from Value / Quantity — and
    the date column is 'Execution date and time', which only a containment match finds."""
    m, _ = map_columns(GROWW_HEADER)
    assert m["date"] == "Execution date and time"
    assert m["symbol"] == "Symbol"          # not ISIN, which is also a synonym
    assert m["name"] == "Stock name"
    assert m["side"] == "Type"
    assert m["quantity"] == "Quantity"
    assert m["amount"] == "Value"
    assert m["status"] == "Order status"
    assert m["exchange"] == "Exchange"
    assert "price" not in m                  # derived, not present


def test_cancelled_and_failed_orders_are_excluded():
    """A statement lists ORDERS, not trades. Importing a cancelled order gives the user a
    position they never held — silently, and in a number they are likely to trust."""
    tsv = _groww_tsv([
        ["Reliance", "RELIANCE", "INE002A01018", "BUY", "10", "12000.00", "NSE", "O1",
         "2026-03-14 09:20:11", "EXECUTED"],
        ["Tata Steel", "TATASTEEL", "INE081A01020", "BUY", "50", "7500.00", "BSE", "O2",
         "2026-04-02 11:00:00", "CANCELLED"],
        ["Infosys", "INFY", "INE009A01021", "BUY", "20", "30000.00", "NSE", "O3",
         "2026-05-02 13:30:00", "FAILED"],
        ["Wipro", "WIPRO", "INE075A01022", "BUY", "5", "2500.00", "NSE", "O4",
         "2026-06-01 10:00:00", "REJECTED"],
    ])
    txns, skipped, _, _, stats = read_transactions(tsv)

    assert [t.symbol for t in txns] == ["RELIANCE.NS"]
    assert stats["filtered_not_executed"] == 3
    assert skipped == []                     # filtered, not failed to parse
    assert stats["status_counts"]["CANCELLED"] == 1


def test_the_exchange_column_picks_the_ticker_suffix():
    """Verified against live data: TATASTEEL.BO and RELIANCE.BO both resolve, so a BSE
    trade must not be labelled .NS."""
    tsv = _groww_tsv([
        ["Tata Steel", "TATASTEEL", "INE081A01020", "BUY", "50", "7500.00", "BSE", "O1",
         "2026-04-02 11:00:00", "EXECUTED"],
        ["Reliance", "RELIANCE", "INE002A01018", "BUY", "10", "12000.00", "NSE", "O2",
         "2026-03-14 09:20:11", "EXECUTED"],
    ])
    txns, _, _, _, _ = read_transactions(tsv)
    assert {t.symbol for t in txns} == {"TATASTEEL.BO", "RELIANCE.NS"}
    assert normalise_symbol("TATASTEEL", exchange="BSE")[0] == "TATASTEEL.BO"
    assert normalise_symbol("TATASTEEL", exchange="NSE")[0] == "TATASTEEL.NS"
    assert normalise_symbol("TATASTEEL")[0] == "TATASTEEL.NS"      # unknown venue


def test_an_unrecognised_status_is_kept_but_surfaced():
    """Dropping a row because its status word is unfamiliar would lose a real trade
    silently. Keep it, and report every status seen so the user can check."""
    tsv = _groww_tsv([
        ["Reliance", "RELIANCE", "INE002A01018", "BUY", "10", "12000.00", "NSE", "O1",
         "2026-03-14 09:20:11", "PART TRADED"],
    ])
    txns, _, _, _, stats = read_transactions(tsv)
    assert len(txns) == 1
    assert stats["status_counts"]["PART TRADED"] == 1


def test_a_file_of_only_cancelled_orders_says_so(tmp_path):
    tsv = _groww_tsv([
        ["Tata Steel", "TATASTEEL", "INE081A01020", "BUY", "50", "7500.00", "BSE", "O1",
         "2026-04-02 11:00:00", "CANCELLED"],
    ])
    with pytest.raises(ImportError_) as e:
        read_transactions(tsv)
    assert "not executed" in str(e.value) and "CANCELLED" in str(e.value)


def test_tab_separated_groww_export_imports_through_read_portfolio(tmp_path):
    import agent
    tsv = _groww_tsv([
        ["Reliance", "RELIANCE", "INE002A01018", "BUY", "10", "12000.00", "NSE", "O1",
         "2026-03-14 09:20:11", "EXECUTED"],
        ["Reliance", "RELIANCE", "INE002A01018", "SELL", "4", "6800.00", "NSE", "O2",
         "2026-08-11 10:05:00", "EXECUTED"],
        ["Tata Steel", "TATASTEEL", "INE081A01020", "BUY", "50", "7500.00", "BSE", "O3",
         "2026-04-02 11:00:00", "CANCELLED"],
    ])
    p = tmp_path / "groww.csv"
    p.write_text(tsv, encoding="utf-8")
    holdings, notes = agent.read_portfolio(str(p))

    assert len(holdings) == 1                       # the cancelled order created nothing
    assert holdings[0]["symbol"] == "RELIANCE.NS"
    assert len(holdings[0]["transactions"]) == 2
    assert holdings[0]["total_invested"] == 12000.00   # gross buys, sale not netted off


# =========================================================================================
# Mutual fund statements: Scheme Name | Transaction Type | Units | NAV | Amount | Date
# No symbol column at all, so the scheme name has to be resolved to an AMFI code.
# =========================================================================================

from xray_agent.adapters.fund_resolver import (pick_best, resolve_holdings, resolve_scheme,
                                               score_candidate, split_scheme_name)

MF_HEADER = ["Scheme Name", "Transaction Type", "Units", "NAV", "Amount", "Date"]


def _mf_tsv(rows):
    return "\t".join(MF_HEADER) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n"


def test_the_mf_header_maps_with_nav_as_the_price():
    m, _ = map_columns(MF_HEADER)
    assert m["name"] == "Scheme Name"
    assert m["side"] == "Transaction Type"
    assert m["quantity"] == "Units"
    assert m["price"] == "NAV"
    assert m["amount"] == "Amount"
    assert m["date"] == "Date"
    assert "symbol" not in m            # there is none; the name must be resolved


def test_purchase_and_redemption_are_understood_as_buy_and_sell():
    txns, _, _, _, _ = read_transactions(_mf_tsv([
        ["Parag Parikh Flexi Cap Fund - Direct Growth", "Purchase", "85.432", "58.52",
         "5000.00", "2026-01-05"],
        ["Parag Parikh Flexi Cap Fund - Direct Growth", "Redemption", "50.000", "66.10",
         "3305.00", "2026-09-15"],
    ]))
    assert [t.side for t in txns] == [BUY, SELL]
    assert txns[0].units == 85.432
    # The price is the cash actually paid per unit (5000 / 85.432), not the 2dp NAV the
    # statement prints, which agrees with it to within the rounding of that NAV.
    assert txns[0].price == pytest.approx(5000.0 / 85.432)
    assert txns[0].price == pytest.approx(58.52, rel=2e-4)


# --- splitting the scheme name ---------------------------------------------------------

def test_scheme_names_split_into_searchable_core_plus_plan_and_option():
    """AMFI's master list has stopped carrying the plan/option qualifier, so searching the
    FULL statement name all-words finds nothing — "direct" and "growth" appear in the query
    and in no master name. Core is what we search; plan and option are what we pick with."""
    p = split_scheme_name("Parag Parikh Flexi Cap Fund - Direct Growth")
    assert p["core"] == "parag parikh flexi cap"
    assert p["plan"] == "direct"
    assert p["options"] == ["growth"]

    q = split_scheme_name("SBI Bluechip Fund Regular Plan Monthly IDCW Payout")
    assert q["core"] == "sbi bluechip"
    assert q["plan"] == "regular"
    assert q["options"] == ["idcw", "payout"]


def test_dividend_is_treated_as_idcw():
    assert split_scheme_name("HDFC Top 100 Direct Dividend")["options"] == ["idcw"]


def test_a_name_with_no_plan_stated_says_so_rather_than_assuming():
    p = split_scheme_name("Axis Midcap Fund")
    assert p["plan"] is None and p["options"] == []
    assert p["core"] == "axis midcap"


# --- picking the right variant ------------------------------------------------------------

DIRECT_G = {"fund_code": "122639", "scheme_name": "PPFC Fund - Direct Plan - Growth"}
REGULAR_G = {"fund_code": "122640", "scheme_name": "PPFC Fund - Regular Plan - Growth"}
DIRECT_IDCW = {"fund_code": "153964",
               "scheme_name": "PPFC Fund - Direct Plan - Monthly IDCW Payout"}
CANDIDATES = [DIRECT_G, DIRECT_IDCW, REGULAR_G]


def test_contradicting_the_stated_plan_is_penalised_not_merely_unrewarded():
    """A Regular-plan holder valued on the Direct NAV gets the wrong expense ratio and
    therefore every return number wrong. Picking the stated plan is not a nicety."""
    assert score_candidate(REGULAR_G["scheme_name"], "regular", ["growth"]) > 0
    assert score_candidate(DIRECT_G["scheme_name"], "regular", ["growth"]) < 0


def test_the_variant_matching_the_statement_is_chosen():
    assert pick_best(CANDIDATES, "direct", ["growth"])["fund_code"] == "122639"
    assert pick_best(CANDIDATES, "regular", ["growth"])["fund_code"] == "122640"
    assert pick_best(CANDIDATES, "direct", ["idcw", "payout"])["fund_code"] == "153964"


def test_no_candidates_resolves_to_nothing_rather_than_a_guess():
    assert pick_best([], "direct", ["growth"]) is None


def test_an_equal_scoring_tie_is_flagged_as_ambiguous():
    twins = [{"fund_code": "1", "scheme_name": "Some Fund"},
             {"fund_code": "2", "scheme_name": "Some Fund"}]
    assert pick_best(twins, None, [])["ambiguous"] is True


# --- resolution end to end (network injected) ---------------------------------------------

def _fake_search(query):
    assert "direct" not in query and "growth" not in query   # core only, never the full name
    return json.dumps({"matches": CANDIDATES, "total_matches": len(CANDIDATES)})


def test_resolve_scheme_searches_the_core_and_picks_on_plan():
    r = resolve_scheme("Parag Parikh Flexi Cap Fund - Regular Plan - Growth",
                       search_fn=_fake_search)
    assert r["fund_code"] == "122640"
    assert r["plan"] == "regular"


def test_resolve_holdings_fills_symbol_and_source_and_reports_what_it_did():
    holdings = [{"name": "Parag Parikh Flexi Cap Fund - Direct Growth", "symbol": None,
                 "source": "", "type": ""}]
    notes = resolve_holdings(holdings, search_fn=_fake_search)
    assert holdings[0]["symbol"] == "122639"
    assert holdings[0]["source"] == "mftool"
    assert holdings[0]["type"] == "Mutual Fund"
    assert "AMFI 122639" in notes[0]


def test_an_already_resolved_holding_is_left_alone():
    holdings = [{"name": "Reliance", "symbol": "RELIANCE.NS", "source": "yfinance",
                 "type": ""}]
    assert resolve_holdings(holdings, search_fn=_fake_search) == []
    assert holdings[0]["symbol"] == "RELIANCE.NS"


def test_a_lookup_failure_degrades_to_unresolved_rather_than_breaking_the_import():
    def boom(_q):
        raise RuntimeError("AMFI unreachable")

    holdings = [{"name": "Axis Midcap Fund", "symbol": None, "source": "", "type": ""}]
    notes = resolve_holdings(holdings, search_fn=boom)
    assert holdings[0]["source"] == ""        # still unpriceable, but the import survives
    assert "AMFI lookup failed" in notes[0]


def test_an_mf_statement_imports_without_touching_the_network(tmp_path):
    """resolve_funds=False keeps the parse hermetic; resolution is a separate concern."""
    from xray_agent.adapters.broker_import import read_broker_csv
    p = tmp_path / "mf.csv"
    p.write_text(_mf_tsv([
        ["Parag Parikh Flexi Cap Fund - Direct Growth", "Purchase", "85.432", "58.52",
         "5000.00", "2026-01-05"],
        ["Parag Parikh Flexi Cap Fund - Direct Growth", "Redemption", "50.000", "66.10",
         "3305.00", "2026-09-15"],
    ]), encoding="utf-8")
    holdings, notes = read_broker_csv(str(p), resolve_funds=False)
    assert len(holdings) == 1
    assert len(holdings[0]["transactions"]) == 2
    assert notes["unresolved_instruments"]      # honest: no code, so no price


# --- the API upload path -------------------------------------------------------------------

TAB_BANNER = ("Groww Order Statement\nAccount: XXXXX\n\n"
              + "\t".join(GROWW_HEADER) + "\n"
              + "\t".join(["Reliance", "RELIANCE", "INE002A01018", "BUY", "10", "12000.00",
                           "NSE", "O1", "2026-03-14 09:20:11", "EXECUTED"]) + "\n")


def test_a_tab_separated_export_with_banner_lines_is_detected():
    """Regression. csv.Sniffer is handed a sample starting with banner text, cannot find a
    consistent delimiter, and falls back to comma — at which point a TAB file parses as one
    column per line and the header is never found. A real Groww export hit exactly this.
    The delimiter is now chosen by which one actually maps a header."""
    txns, _, mapping, _, _ = read_transactions(TAB_BANNER)
    assert len(txns) == 1
    assert mapping["quantity"] == "Quantity"
    assert txns[0].symbol == "RELIANCE.NS"


@pytest.mark.parametrize("delim", [",", "\t", ";", "|"])
def test_every_supported_delimiter_round_trips(delim):
    hdr = delim.join(["Trade Date", "Symbol", "Type", "Quantity", "Price"])
    row = delim.join(["2026-03-14", "RELIANCE", "BUY", "10", "1200"])
    txns, _, _, _, _ = read_transactions(f"Some Banner\n\n{hdr}\n{row}\n")
    assert len(txns) == 1 and txns[0].units == 10


def test_upload_validation_accepts_every_supported_format(tmp_path):
    """_csv_to_temp used to run its OWN pd.read_csv, which raised ParserError (not a
    ValueError, so it escaped the friendly-error path) on any statement with banner
    lines. It now validates with the same parser the engine uses."""
    import api
    import agent
    import os
    sip = ("Mode,App,Type,Market,Risk,Where,symbol,source,Total Invested,Mar'25\n"
           "Equity,Groww,Share,India,Med,Reliance,RELIANCE.NS,yfinance,5000,5000\n")
    for label, text in [("tab statement", TAB_BANNER), ("sip sheet", sip),
                        ("mf statement", _mf_tsv([
                            ["Axis Midcap Fund Direct Growth", "Purchase", "10", "50",
                             "500", "2026-01-05"]]))]:
        path = api._csv_to_temp(text)
        try:
            holdings, _ = agent.read_portfolio(path, resolve_funds=False)
            assert holdings, label
        finally:
            os.unlink(path)


def test_upload_validation_rejects_junk_with_a_friendly_error():
    import api
    with pytest.raises(ValueError) as e:
        api._csv_to_temp("Alpha,Beta\n1,2\n")
    assert "missing required column" in str(e.value)


def test_the_holdings_cap_counts_holdings_not_transactions():
    """len(df) counted ROWS, so 120 trades in a single stock were rejected as
    "120 holdings". A statement legitimately has far more rows than positions."""
    import api
    import agent
    import os
    many = ("Trade Date,Symbol,Type,Quantity,Price\n"
            + "\n".join(f"2026-03-{(i % 28) + 1:02d},RELIANCE,BUY,1,1200"
                        for i in range(api.HOLDINGS_CAP * 2)) + "\n")
    path = api._csv_to_temp(many)          # must NOT raise
    try:
        holdings, _ = agent.read_portfolio(path, resolve_funds=False)
        assert len(holdings) == 1
    finally:
        os.unlink(path)


def test_the_holdings_cap_still_rejects_too_many_real_holdings():
    import api
    rows = "\n".join(f"2026-03-14,SYM{i},BUY,1,100" for i in range(api.HOLDINGS_CAP + 5))
    with pytest.raises(ValueError) as e:
        api._csv_to_temp("Trade Date,Symbol,Type,Quantity,Price\n" + rows + "\n")
    assert "caps portfolios" in str(e.value)


def test_validation_does_not_hit_the_network_for_fund_codes(monkeypatch):
    """resolve_funds=False on the validation read: AMFI lookups belong in the endpoint
    that actually values things, not in a size check that runs on every upload."""
    import api
    import os
    from xray_agent.adapters import fund_resolver

    def explode(*a, **kw):
        raise AssertionError("validation must not resolve fund codes")

    monkeypatch.setattr(fund_resolver, "resolve_holdings", explode)
    path = api._csv_to_temp(_mf_tsv([
        ["Axis Midcap Fund Direct Growth", "Purchase", "10", "50", "500", "2026-01-05"]]))
    os.unlink(path)


# --- data retention ------------------------------------------------------------------------

def test_the_generated_report_is_deleted_after_it_is_sent():
    """The workbook holds the user's ENTIRE portfolio - holdings, values, P&L, charts.
    It cannot be unlinked before FileResponse streams it, and the endpoint's finally only
    removed the uploaded CSV, so every report left a complete portfolio on the server's
    disk for good. A BackgroundTask now removes it once the response has been sent."""
    import inspect
    import api
    src = inspect.getsource(api._report_compute)
    assert "background=BackgroundTask(_quiet_unlink, out.name)" in src
    # and if generation raises, no response carries that task, so it must be cleaned inline
    assert "if not delivered:" in src


def test_the_uploaded_csv_is_deleted_on_every_upload_endpoint():
    import inspect
    import api
    for fn in (api.overview, api._analysis_compute, api._report_compute):
        src = inspect.getsource(fn)
        assert "if is_temp:" in src and "_quiet_unlink(path)" in src, fn.__name__


def test_rate_limit_rows_do_not_accumulate_ip_addresses_for_ever():
    """An IP is personal data and the counter only needs today's row."""
    import inspect
    import api
    assert "DELETE FROM rate_limit WHERE day < ?" in inspect.getsource(api._bump)


def test_the_query_log_records_no_portfolio_content():
    """One row per query: category, tool NAMES, model names, token counts, cost, latency.
    No message text, no holdings, no amounts — that is what makes 'nothing stored' true."""
    import logger
    cols = {c.strip().split()[0] for c in logger.init_db.__doc__ or ""} if False else None
    import sqlite3, tempfile, os
    f = os.path.join(tempfile.mkdtemp(), "t.db")
    logger.init_db(f)
    names = {r[1] for r in sqlite3.connect(f).execute("PRAGMA table_info(query_log)")}
    assert names == {"id", "ts", "category", "tools", "models", "input_tokens",
                     "output_tokens", "est_cost_usd", "latency_ms"}
    for leaky in ("question", "query", "answer", "message", "holdings", "portfolio"):
        assert leaky not in names


def test_stats_survives_a_db_that_only_the_investigator_has_written():
    """/stats 500'd on a fresh checkout. summarize() guarded on the FILE existing, but
    the Investigator's run store creates xray.db too — so one `xray_agent history` call
    before the first chat leaves a db with investigation_runs and no query_log, and the
    aggregate raised `no such table`. Absent table means nothing logged, not a crash."""
    import logger
    import sqlite3, tempfile, os
    f = os.path.join(tempfile.mkdtemp(), "t.db")
    with sqlite3.connect(f) as conn:            # what the run store leaves behind
        conn.execute("CREATE TABLE investigation_runs (id INTEGER PRIMARY KEY)")
    assert os.path.exists(f)
    assert logger.summarize(f) == {"queries": 0}


# --- classifying holdings a statement does not classify ---------------------------------------

def test_a_statement_stock_is_a_share_not_other():
    """The allocation chart, the sector section and the direct look-through all filter on
    type == "share". A statement gave every holding type "" so a book built from one
    looked like a pile of "Other" and those sections had nothing to say."""
    h = to_holdings([T(date(2026, 1, 5), BUY, 10, 100, symbol="RELIANCE.NS")])[0]
    assert (h["type"], h["market"]) == ("Share", "India")


def test_a_us_ticker_is_a_us_share_and_a_crypto_pair_is_crypto():
    from xray_agent.adapters.broker_import import infer_instrument
    assert infer_instrument("AAPL", "yfinance", "Apple") == ("Share", "US")
    assert infer_instrument("BTC-USD", "crypto", "Bitcoin") == ("Crypto", "Global")


def test_an_amfi_scheme_is_a_mutual_fund_in_india():
    from xray_agent.adapters.broker_import import infer_instrument
    assert infer_instrument("122639", "mftool", "Parag Parikh Flexi Cap") == (
        "Mutual Fund", "India")


def test_a_bees_or_etf_name_is_an_etf():
    from xray_agent.adapters.broker_import import infer_instrument
    assert infer_instrument("NIFTYBEES.NS", "yfinance", "Nippon Nifty BeES")[0] == "ETF"
    assert infer_instrument("GOLDBEES.NS", "yfinance", "Nippon Gold ETF")[0] == "ETF"
    assert infer_instrument("TCS.NS", "yfinance", "TCS")[0] == "Share"


def test_an_unresolved_scheme_is_left_unclassified_rather_than_guessed():
    from xray_agent.adapters.broker_import import infer_instrument
    assert infer_instrument("", "", "Some Fund Direct Growth") == ("", "")
