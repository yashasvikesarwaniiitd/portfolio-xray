"""The health report's beta and fee estimate must match the rules the rest of the app follows.

  * Beta is against the holding's OWN market (NIFTY 50 for .NS/.BO, S&P 500 otherwise) and the
    two series are joined on calendar DATE. The report kept its own copy that regressed everything
    on the NIFTY by timestamp, so US and crypto holdings quietly came back with no beta and the
    portfolio figure described only the Indian slice.
  * The 10-year fee estimate is for the funds whose fee is known. It was applied to the WHOLE
    portfolio value, direct stocks included, which pay no expense ratio.

All figures invented. Hermetic: yfinance is replaced by fixed series.
"""
import numpy as np
import pandas as pd
import pytest

import agent
from report import data as rd

N = 300


def market_returns(seed):
    return np.random.default_rng(seed).normal(0.0005, 0.01, N)


def closes_from(returns, start=100.0):
    out, x = [], start
    for r in returns:
        x *= 1 + r
        out.append(x)
    return out


def hist(tz, closes):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B", tz=tz)
    return pd.DataFrame({"Close": closes}, index=idx)


class FakeTicker:
    table: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, period="2y"):
        return self.table.get(self.symbol, pd.DataFrame({"Close": []}))


@pytest.fixture
def fake_yf(monkeypatch):
    FakeTicker.table = {}
    monkeypatch.setattr(rd.yf, "Ticker", FakeTicker)
    return FakeTicker


def row(symbol, source="yfinance", weight=50.0, name=None):
    return {"symbol": symbol, "source": source, "weight_pct": weight, "risk": "High",
            "name": name or symbol, "type": "Share", "value": weight * 1000}


def test_a_us_holding_gets_a_beta_against_the_sp500_not_the_nifty(fake_yf):
    sp = market_returns(1)
    nifty = market_returns(2)
    fake_yf.table = {
        agent.NIFTY50_SYMBOL: hist("Asia/Kolkata", closes_from(nifty)),
        "^GSPC": hist("America/New_York", closes_from(sp)),
        "AAPL": hist("America/New_York", closes_from(1.5 * sp)),         # exactly 1.5x S&P
        "RELIANCE.NS": hist("Asia/Kolkata", closes_from(0.8 * nifty)),   # exactly 0.8x NIFTY
    }
    out = rd.section_risk([row("AAPL"), row("RELIANCE.NS")])
    betas = {b["name"]: b["beta"] for b in out["beta_ladder"]}
    assert betas == {"AAPL": pytest.approx(1.5, abs=0.01), "RELIANCE.NS": pytest.approx(0.8, abs=0.01)}
    assert out["portfolio_beta_est"] == pytest.approx(1.15, abs=0.01)
    assert out["beta_coverage_pct_of_portfolio"] == 100.0


def test_series_stamped_in_different_timezones_are_matched_by_date(fake_yf):
    """Crypto is stamped UTC and the S&P in New York: the same trading day carries different
    timestamps, so joining on the timestamp matches almost nothing."""
    sp = market_returns(3)
    fake_yf.table = {
        "^GSPC": hist("America/New_York", closes_from(sp)),
        "BTC-USD": hist("UTC", closes_from(2.0 * sp)),
    }
    out = rd.section_risk([row("BTC-USD", source="crypto", weight=100.0)])
    assert [b["beta"] for b in out["beta_ladder"]] == [pytest.approx(2.0, abs=0.01)]


def test_a_missing_benchmark_drops_only_the_holdings_that_need_it(fake_yf):
    nifty = market_returns(4)
    fake_yf.table = {                                  # no S&P series at all
        agent.NIFTY50_SYMBOL: hist("Asia/Kolkata", closes_from(nifty)),
        "RELIANCE.NS": hist("Asia/Kolkata", closes_from(nifty)),
        "AAPL": hist("America/New_York", closes_from(nifty)),
    }
    out = rd.section_risk([row("AAPL", weight=40.0), row("RELIANCE.NS", weight=60.0)])
    assert [b["name"] for b in out["beta_ladder"]] == ["RELIANCE.NS"]
    assert out["beta_coverage_pct_of_portfolio"] == 60.0     # says what it covers, not 100


# --- the fee estimate -------------------------------------------------------------------------

def fund(weight):
    return {"symbol": "ETF1", "source": "yfinance", "type": "ETF", "name": "An ETF",
            "weight_pct": weight, "value": weight * 1000}


def test_the_ten_year_fee_estimate_is_on_the_funds_not_the_whole_portfolio(monkeypatch):
    monkeypatch.setattr(rd, "fetch_meta", lambda sym, cache: {"expense_ratio_pct": 1.0})
    stock = {"symbol": "TCS.NS", "source": "yfinance", "type": "Share", "name": "TCS",
             "weight_pct": 80.0, "value": 80000.0}
    out = rd.section_cost([fund(20.0), stock], {}, 100000.0)
    # 20% of the portfolio is in a fund that charges 1%; the other 80% pays no expense ratio.
    assert out["fees_10yr_est"] == round(20000 * ((1.01) ** 10 - 1), 0)
    assert out["portfolio_value_for_feedrag"] == 20000        # what the chart and caption use


def test_a_portfolio_of_only_covered_funds_is_unchanged(monkeypatch):
    monkeypatch.setattr(rd, "fetch_meta", lambda sym, cache: {"expense_ratio_pct": 0.5})
    out = rd.section_cost([fund(100.0)], {}, 50000.0)
    assert out["fees_10yr_est"] == round(50000 * ((1.005) ** 10 - 1), 0)
    assert out["portfolio_value_for_feedrag"] == 50000
