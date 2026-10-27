"""Regression tests for a portfolio built from real broker statements that showed a loss of
~Rs 1 lakh when the true figure was ~Rs 8,000.

All figures here are invented. The failures were:

  * a mutual fund priced against the WRONG AMFI scheme (a fund with a NAV of ~300 priced on an
    index fund with a NAV of ~10), fabricating a ~96% loss on each of two holdings;
  * one stock held on both NSE and BSE counted as two holdings, priced a day apart;
  * profit realised by selling absent from every total;
  * the portfolio XIRR dating each purchase to the 1st of its month and dropping every sale.

Each test names the failure it would have let through. Hermetic: no network.
"""
from datetime import date, timedelta

import pytest

import agent
from xray_agent.adapters import broker_import as bi
from xray_agent.adapters import fund_resolver as fr
from xray_agent.domain.transactions import BUY, SELL, Transaction


def T(d, side, units, price, symbol="X.NS", name="X", isin=""):
    return Transaction(date=d, side=side, units=units, price=price, symbol=symbol,
                       name=name, isin=isin)


def series(first, days, start_nav, step=0.0):
    """A NAV history: one point per day from `first`."""
    return {first + timedelta(days=i): round(start_nav + step * i, 4) for i in range(days)}


# --- NAV corroboration -----------------------------------------------------------------------

def test_the_right_scheme_matches_every_purchase_and_a_different_fund_matches_none():
    d0 = date(2025, 1, 1)
    real = series(d0, 400, 300.0, 0.5)             # a ~300 NAV
    index_fund = series(d0, 400, 10.0, 0.01)       # a ~10 NAV
    buys = [T(d0 + timedelta(days=k), BUY, 1, real[d0 + timedelta(days=k)])
            for k in (10, 100, 200, 300)]
    assert fr.nav_match(real, buys) == (4, 4)
    assert fr.nav_match(index_fund, buys) == (0, 4)


def test_a_nav_applied_the_next_business_day_still_matches():
    """The statement's date is the order date; the NAV can be the next business day's."""
    d0 = date(2025, 1, 1)
    s = series(d0, 60, 100.0, 1.0)
    t = T(d0 + timedelta(days=20), BUY, 1, s[d0 + timedelta(days=22)])
    assert fr.nav_match(s, [t]) == (1, 1)


def test_a_purchase_before_the_first_published_nav_is_untestable_not_a_mismatch():
    """A new-fund offer is allotted at Rs 10 days before the scheme's NAV history starts.
    Counting it as a mismatch rejected a correctly identified fund."""
    d0 = date(2025, 10, 17)
    s = series(d0, 100, 10.09, 0.01)
    nfo = T(date(2025, 10, 13), BUY, 300, 10.0)
    later = T(d0 + timedelta(days=30), BUY, 100, s[d0 + timedelta(days=30)])
    assert fr.nav_match(s, [nfo, later]) == (1, 1)
    assert fr.nav_untestable(s, [nfo, later]) == 1


# --- candidate selection ---------------------------------------------------------------------

MASTER = {
    "100": "Kotak Small Cap Fund",
    "101": "Kotak Small Cap Fund",
    "200": "Kotak Nifty SmallCap 250 Index Fund",
    "201": "Kotak Nifty SmallCap 250 Index Fund",
    "300": "Motilal Oswal Midcap Fund",
    "301": "Motilal Oswal Large and Midcap Fund",
    "302": "Motilal Oswal BSE Midcap 150 Momentum 30 Index Fund",
    "400": "Axis Global Equity Alpha Fund of Fund",
    "500": "Tata Digital India Fund",
    "501": "Tata Nifty India Digital ETF Fund of Fund",
}


def codes(name):
    return sorted(c for c, _ in fr.candidate_pool(name, MASTER))


def test_a_different_fund_that_merely_contains_the_words_is_not_a_candidate():
    """The incident. An all-words search returned the index fund beside the real one, then
    sorted it first."""
    assert codes("Kotak Small Cap Fund Direct Growth") == ["100", "101"]
    assert codes("Motilal Oswal Midcap Fund Direct Growth") == ["300"]
    assert codes("Tata Digital India Fund Direct Growth") == ["500"]


def test_spacing_and_abbreviation_do_not_hide_the_right_fund():
    """"Small Cap" vs "SmallCap" and "FoF" vs "Fund of Fund" - the second is why one fund
    was dropped from the portfolio altogether."""
    assert codes("Kotak SmallCap Fund Direct Growth") == ["100", "101"]
    assert codes("Axis Global Equity Alpha FoF Direct Growth") == ["400"]


# --- verified resolution ---------------------------------------------------------------------

D0 = date(2025, 1, 1)
REAL = series(D0, 400, 300.0, 0.5)
INDEX = series(D0, 400, 10.0, 0.01)


def holding(name, nav_series_for_buys, days=(10, 100, 200)):
    txns = [T(D0 + timedelta(days=k), BUY, 1, nav_series_for_buys[D0 + timedelta(days=k)],
              symbol=name.upper(), name=name) for k in days]
    return {"name": name, "symbol": name.upper(), "source": "", "type": "", "market": "",
            "risk": "", "total_invested": 0.0, "inflows": [], "transactions": txns}


def resolve(h, histories, details=None):
    return fr.resolve_holdings(
        [h], master_fn=lambda: MASTER, nav_fn=lambda c: histories.get(c, {}),
        detail_fn=lambda c: (details or {}).get(c, ""))


def test_the_scheme_whose_navs_match_the_statement_is_chosen():
    h = holding("Kotak Small Cap Fund Direct Growth", REAL)
    notes = resolve(h, {"100": REAL, "101": INDEX})
    assert h["symbol"] == "100" and h["source"] == "mftool"
    assert "verified: 3 of 3" in notes[0]


def test_it_beats_a_better_looking_name_when_the_navs_disagree():
    """Even if the wrong scheme were the only one offered, it must be refused rather than
    used - a fund with no matching NAVs is reported unpriced, never priced on a guess."""
    h = holding("Kotak Small Cap Fund Direct Growth", REAL)
    notes = resolve(h, {"100": INDEX, "101": INDEX})
    assert h["source"] == "" and h["symbol"] == "KOTAK SMALL CAP FUND DIRECT GROWTH"
    assert "not pricing it on a guess" in notes[0]


def test_growth_and_direct_variants_are_told_apart_by_their_navs():
    """Direct and Regular carry different NAVs, so the statement identifies the plan
    without relying on a name the master list often drops."""
    regular = {d: v * 0.98 for d, v in REAL.items()}
    h = holding("Kotak Small Cap Fund Direct Growth", REAL)
    resolve(h, {"100": regular, "101": REAL})
    assert h["symbol"] == "101"


def test_identical_navs_fall_back_to_the_plan_the_statement_asked_for_and_say_so():
    h = holding("Kotak Small Cap Fund Direct Growth", REAL)
    notes = resolve(h, {"100": REAL, "101": REAL},
                    details={"100": "Kotak Small Cap Fund - Regular Plan - Growth",
                             "101": "Kotak Small Cap Fund - Direct Plan - Growth"})
    assert h["symbol"] == "101"
    assert "AMBIGUOUS" not in notes[0], "the plan separates them, so it is not ambiguous"


def test_a_new_fund_offer_allotment_does_not_fail_a_correctly_identified_fund():
    first = date(2025, 10, 17)
    s = series(first, 200, 10.09, 0.01)
    nfo = T(date(2025, 10, 13), BUY, 300, 10.0, symbol="KOTAK SMALL CAP FUND DIRECT GROWTH",
            name="Kotak Small Cap Fund Direct Growth")
    ok = T(first + timedelta(days=40), BUY, 100, s[first + timedelta(days=40)],
           symbol=nfo.symbol, name=nfo.name)
    h = {"name": nfo.name, "symbol": nfo.symbol, "source": "", "type": "", "market": "",
         "risk": "", "total_invested": 0.0, "inflows": [], "transactions": [nfo, ok]}
    notes = resolve(h, {"100": s, "101": INDEX})
    assert h["symbol"] == "100"
    assert "predate its first published NAV" in notes[0]


def test_a_failed_nav_fetch_leaves_the_fund_unresolved_not_guessed():
    h = holding("Kotak Small Cap Fund Direct Growth", REAL)

    def boom(code):
        raise RuntimeError("AMFI down")

    notes = fr.resolve_holdings([h], master_fn=lambda: MASTER, nav_fn=boom,
                                detail_fn=lambda c: "")
    assert h["source"] == "" and "not pricing it" in notes[0]


# --- one position per instrument -------------------------------------------------------------

def test_a_stock_bought_on_nse_and_bse_is_one_holding_priced_on_nse():
    """Two holdings, two prices a day apart, a split P&L and a doubled holding count."""
    isin = "INE467B01029"
    hs = bi.to_holdings([
        T(date(2025, 9, 19), BUY, 3, 3158, symbol="TCS.BO", name="TATA CONSULTANCY", isin=isin),
        T(date(2025, 9, 22), BUY, 1, 3081, symbol="TCS.NS", name="TATA CONSULTANCY", isin=isin),
        T(date(2025, 10, 6), BUY, 1, 2984, symbol="TCS.BO", name="TATA CONSULTANCY", isin=isin),
    ])
    assert len(hs) == 1
    assert hs[0]["symbol"] == "TCS.NS", "priced on NSE"
    assert sum(t.units for t in hs[0]["transactions"]) == 5


def test_a_sell_on_one_venue_closes_a_buy_made_on_the_other():
    """FIFO is per instrument, not per exchange: Indian capital gains do not care which
    venue a lot was bought on."""
    isin = "INE0MHO01029"
    hs = bi.to_holdings([
        T(date(2025, 8, 19), BUY, 100, 100, symbol="REG.BO", name="REGAAL", isin=isin),
        T(date(2025, 8, 20), SELL, 100, 130, symbol="REG.NS", name="REGAAL", isin=isin),
    ])
    assert len(hs) == 1
    from xray_agent.domain import transactions as txn
    s = txn.summarise(hs[0]["transactions"], date(2025, 9, 1), None)
    assert s["realised_pnl"] == pytest.approx(3000.0)
    assert not s.get("unmatched_units"), "the BSE lot must be matched by the NSE sale"


def test_without_an_isin_the_bare_ticker_still_groups_the_two_venues():
    hs = bi.to_holdings([
        T(date(2025, 1, 5), BUY, 1, 100, symbol="INFY.NS", name="Infosys"),
        T(date(2025, 1, 6), BUY, 1, 100, symbol="INFY.BO", name="Infosys"),
    ])
    assert len(hs) == 1


def test_an_inf_isin_is_an_etf_and_an_ine_isin_is_a_share():
    """Groww names ETFs like "HDFCAMC - HDFCSML250" - no "ETF" in them - so the name
    heuristic filed them as ordinary shares."""
    assert bi.infer_instrument("HDFCSML250.NS", "yfinance", "HDFCAMC - HDFCSML250",
                               "INF179KC1FB2")[0] == "ETF"
    assert bi.infer_instrument("TCS.NS", "yfinance", "TCS", "INE467B01029")[0] == "Share"


def test_the_isin_column_is_read_and_a_malformed_one_is_ignored():
    text = ("Stock name,Symbol,ISIN,Type,Quantity,Value,Exchange,Execution date and time,"
            "Order status\n"
            "TCS,TCS,INE467B01029,BUY,1,3000,NSE,19-09-2025 11:02 AM,Executed\n"
            "ODD,ODD,not-an-isin,BUY,1,100,NSE,19-09-2025 11:02 AM,Executed\n")
    txns = bi.read_transactions(text)[0]
    assert [t.isin for t in txns] == ["INE467B01029", ""]


# --- realised P&L and XIRR -------------------------------------------------------------------

def test_realised_profit_is_reported_beside_the_unrealised_figure():
    rows = [
        {"status": "priced", "invested": 1000.0, "current_value": 900.0, "realised_pnl": 50.0},
        {"status": "closed", "invested": 500.0, "realised_pnl": 200.0},
        {"status": "unavailable", "invested": 300.0},
    ]
    s = agent.aggregate_snapshot(rows)
    assert s["total_pnl_abs"] == -100.0, "unrealised, unchanged"
    assert s["total_realised_pnl"] == 250.0, "includes the fully exited holding"
    assert s["total_pnl_incl_realised"] == 150.0
    assert s["closed_count"] == 1


def test_portfolio_xirr_counts_sale_proceeds_and_uses_the_real_dates():
    """Buy 100 on day 0, sell 110 exactly a year later: 10%. Dating the buy to the 1st of its
    month and dropping the sale gave nonsense."""
    buy_day, sell_day = date(2025, 3, 17), date(2026, 3, 17)
    h = {"inflows": [(2025, 3, 100.0)],
         "transactions": [T(buy_day, BUY, 1, 100), T(sell_day, SELL, 1, 110)]}
    r = {"status": "closed", "price_date": "2026-03-17"}
    # Two identical holdings: a single closed holding alone has too few flows to be a
    # meaningful pool, and the rate is the same for any number of copies.
    assert agent._portfolio_xirr([h, h], [r, dict(r)]) == pytest.approx(10.0, abs=0.05)


def test_an_open_position_contributes_its_current_value_as_the_closing_flow():
    buy_day = date(2025, 1, 1)
    h = {"inflows": [(2025, 1, 100.0)], "transactions": [T(buy_day, BUY, 1, 100)]}
    r = {"status": "priced", "price_date": "2027-01-01", "current_value": 121.0,
         "current_price": 121.0}
    assert agent._portfolio_xirr([h], [r]) == pytest.approx(10.0, abs=0.05)


def test_a_bse_only_stock_is_priced_from_bse_when_nse_has_no_series(monkeypatch):
    calls = []

    def fake(source, symbol, start, end):
        calls.append(symbol)
        return {} if symbol.endswith(".NS") else {date(2026, 1, 2): 50.0}

    monkeypatch.setattr(agent, "_price_map", fake)
    h = {"name": "OnlyBSE", "symbol": "ONLYBSE.NS", "source": "yfinance", "type": "Share",
         "market": "India", "risk": "", "total_invested": 100.0, "inflows": [],
         "transactions": [T(date(2025, 12, 1), BUY, 2, 40, symbol="ONLYBSE.NS")]}
    row = agent._value_from_transactions(h)
    assert row["status"] == "priced" and row["current_value"] == 100.0
    assert calls == ["ONLYBSE.NS", "ONLYBSE.BO"]


# --- the benchmark ---------------------------------------------------------------------------

def flat_index(first, days, level=100.0):
    return {first + timedelta(days=i): level for i in range(days)}


def test_the_same_money_in_a_flat_index_returns_nothing():
    """Hand check: an index that never moves returns 0%, whatever the timing of the flows."""
    idx = flat_index(date(2025, 1, 1), 800)
    flows = [(date(2025, 2, 1), -1000.0), (date(2025, 6, 1), -500.0)]
    assert agent.index_equivalent_xirr(flows, date(2026, 12, 1), idx) == pytest.approx(0.0,
                                                                                    abs=0.01)


def test_an_index_that_doubles_in_a_year_returns_100_percent_for_money_in_at_the_start():
    d0 = date(2025, 1, 1)
    idx = {d0 + timedelta(days=i): 100.0 + 100.0 * i / 365.0 for i in range(366)}
    r = agent.index_equivalent_xirr([(d0, -1000.0)], d0 + timedelta(days=365), idx)
    assert r == pytest.approx(100.0, abs=0.5)


def test_money_that_went_in_late_gets_the_late_index_level_not_the_first_purchase_level():
    """The reason for replaying the flows: all the money goes in on day 300 of an index that
    doubled over the year. A point-to-point figure says +100%; the money earned only the last
    65 days of it."""
    d0 = date(2025, 1, 1)
    idx = {d0 + timedelta(days=i): 100.0 + 100.0 * i / 365.0 for i in range(366)}
    late = agent.index_equivalent_xirr([(d0 + timedelta(days=300), -1000.0)],
                                       d0 + timedelta(days=365), idx)
    point_to_point = (idx[d0 + timedelta(days=365)] / idx[d0] - 1) * 100
    assert point_to_point == pytest.approx(100.0, abs=0.5)
    assert late < 100.0 and late > 0.0


def test_a_sale_takes_index_units_out_again():
    """Buy 1000, sell 1000 the same day: no exposure, no return, no units left."""
    idx = flat_index(date(2025, 1, 1), 400)
    flows = [(date(2025, 3, 1), -1000.0), (date(2025, 3, 1), 1000.0)]
    assert agent.index_equivalent_xirr(flows, date(2025, 12, 1), idx) is None


def test_no_index_data_means_no_benchmark_rather_than_a_made_up_one():
    assert agent.index_equivalent_xirr([(date(2025, 1, 1), -100.0)], date(2025, 6, 1), {}) is None


def test_portfolio_flows_split_the_trades_from_the_closing_value():
    h = {"inflows": [], "transactions": [T(date(2025, 1, 1), BUY, 1, 100),
                                         T(date(2025, 6, 1), SELL, 1, 120)]}
    closed = {"status": "closed", "price_date": "2025-12-01"}
    trade, terminal = agent._portfolio_flows([h], [closed])
    assert trade == [(date(2025, 1, 1), -100.0), (date(2025, 6, 1), 120.0)]
    assert terminal == [], "a fully exited holding has no closing value"


# --- cost exactness --------------------------------------------------------------------------

def test_cost_uses_the_amount_paid_not_units_times_a_rounded_nav():
    """NAV is printed to 2dp, so units x NAV is not what was paid. Rs 4,999 for 108.31 units
    at a printed NAV of 46.16 is 4,999.67."""
    text = ("Scheme Name,Transaction Type,Units,NAV,Amount,Date\n"
            "Some Fund Direct Growth,PURCHASE,108.31,46.16,\"4,999\",06 Oct 2026\n")
    t = bi.read_transactions(text)[0][0]
    assert t.units * t.price == pytest.approx(4999.0, abs=0.001)


def test_an_amount_that_disagrees_with_units_times_price_does_not_rewrite_the_price():
    """A fee-inclusive total, or a different currency, must not silently change the price."""
    text = ("Date,Symbol,Quantity,Price,Amount,Type\n"
            "2026-01-05,XYZ,10,100,5000,BUY\n")
    t = bi.read_transactions(text)[0][0]
    assert t.price == 100.0


# --- the index must not be converted as if it were a dollar price ----------------------------

def test_an_index_level_is_never_converted_to_rupees():
    """^NSEI has no .NS suffix, so it was taken for a US-listed ticker and multiplied by
    USD/INR - NIFTY 50 at ~2,160,000 - and the FX drift flipped the sign of its return."""
    assert not agent.is_usd_denominated("yfinance", "^NSEI")
    assert not agent.is_usd_denominated("yfinance", "^GSPC")
    assert agent.is_usd_denominated("yfinance", "AAPL"), "real dollar tickers still convert"
    assert agent.is_usd_denominated("crypto", "BTC-USD")


# --- a sold holding is closed, not unpriceable -----------------------------------------------

def test_a_fully_sold_holding_is_not_reported_as_something_that_could_not_be_valued():
    """The dashboard said "Some holdings can't be valued Rs 16,000" about a position the
    reader had sold, and "1 of 2 holdings priced" about a book with one open position."""
    rows = [{"status": "priced", "invested": 1000.0, "current_value": 900.0},
            {"status": "closed", "invested": 16000.0, "realised_pnl": 1500.0},
            {"status": "unavailable", "invested": 300.0}]
    s = agent.aggregate_snapshot(rows)
    assert s["unpriced_invested"] == 300.0, "only the genuinely unpriceable holding"
    assert s["closed_count"] == 1
