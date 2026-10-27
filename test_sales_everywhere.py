"""Every reading of a holding must see the SALES in a broker statement, not just the buys.

A statement holding keeps `inflows` (its buys, by month) only so older code degrades instead
of breaking. Sharpe and the weekly digest still rebuilt units from that list, so a holding
bought and then fully sold was "priced" at the value of everything ever bought - in the digest a
fully exited position showed as a live holding with a value. The snapshot read the
transactions and was right; these two did not.

All figures invented. Hermetic: prices are a fixed series.
"""
from datetime import date, timedelta

import pytest

import agent
from xray_agent.domain.transactions import BUY, SELL, Transaction

D0 = date(2026, 1, 5)


def T(d, side, units, price):
    return Transaction(date=d, side=side, units=units, price=price, symbol="X.NS", name="X",
                       isin="")


def holding(trades):
    return {"name": "X", "symbol": "X.NS", "source": "yfinance", "type": "Share",
            "market": "India", "risk": "", "transactions": trades,
            "total_invested": sum(t.value for t in trades if t.side == BUY),
            "inflows": [(t.date.year, t.date.month, t.value) for t in trades if t.side == BUY]}


@pytest.fixture
def prices(monkeypatch):
    """~400 trading days ending today, 100 rising to ~140, so a Sharpe window exists."""
    end = date.today()
    start = end - timedelta(days=400)
    pmap = {start + timedelta(days=i): 100 + i * 0.1 for i in range(401)}
    monkeypatch.setattr(agent, "_price_map", lambda *a, **k: dict(pmap))
    return pmap


def start_date(prices):
    return min(prices) + timedelta(days=20)


# --- the weekly digest ------------------------------------------------------------------------

def test_a_fully_sold_holding_is_not_a_live_holding_in_the_digest(prices):
    d = start_date(prices)
    h = holding([T(d, BUY, 100, 110), T(d + timedelta(days=30), SELL, 100, 115)])
    row = agent._digest_holding(h)
    assert row["status"] != "priced", row
    assert "value" not in row


def test_a_partly_sold_holding_is_valued_on_what_is_still_held(prices):
    d = start_date(prices)
    h = holding([T(d, BUY, 100, 110), T(d + timedelta(days=30), SELL, 40, 115)])
    row = agent._digest_holding(h)
    latest = prices[max(prices)]
    assert row["status"] == "priced"
    assert row["value"] == pytest.approx(60 * latest, abs=0.01)     # 60 left, not 100


def test_the_digest_total_leaves_out_a_fully_sold_holding(prices, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_DIGEST_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(agent.news, "fetch_news", lambda *a, **k: {"articles": [], "count": 0})
    d = start_date(prices)
    held = holding([T(d, BUY, 10, 110)])
    sold = holding([T(d, BUY, 100, 110), T(d + timedelta(days=30), SELL, 100, 115)])
    sold["name"] = "Sold"
    monkeypatch.setattr(agent, "read_portfolio", lambda p: ([held, sold], []))
    out = agent.build_digest("x")
    assert out["highlights"]["total_current_value"] == pytest.approx(
        10 * prices[max(prices)], abs=0.01)
    assert [r["name"] for r in out["holdings"]] == ["X"]


def test_a_sip_sheet_holding_is_still_valued_from_its_inflows(prices):
    """The change must not touch the sheet format, which has only monthly amounts."""
    d = start_date(prices)
    h = {"name": "S", "symbol": "S.NS", "source": "yfinance", "type": "Share",
         "market": "India", "risk": "", "total_invested": 1000.0,
         "inflows": [(d.year, d.month, 1000.0)]}
    row = agent._digest_holding(h)
    assert row["status"] == "priced" and row["value"] > 0


# --- Sharpe -----------------------------------------------------------------------------------

def test_sharpe_gives_a_fully_sold_holding_no_value_and_no_weight(prices):
    d = start_date(prices)
    h = holding([T(d, BUY, 100, 110), T(d + timedelta(days=30), SELL, 100, 115)])
    value, series = agent._holding_series_and_value(h)
    assert not value


def test_sharpe_values_a_partly_sold_holding_on_what_is_still_held(prices):
    d = start_date(prices)
    h = holding([T(d, BUY, 100, 110), T(d + timedelta(days=30), SELL, 40, 115)])
    value, series = agent._holding_series_and_value(h)
    assert value == pytest.approx(60 * prices[max(prices)], abs=0.01)
    assert len(series) >= 150


# --- the exchange-rate cache ------------------------------------------------------------------

class _FakeFx:
    """USD/INR history: one point per day over whatever window is asked for."""
    calls: list = []

    def __init__(self, symbol):
        pass

    def history(self, start, end):
        import pandas as pd
        _FakeFx.calls.append((start, end))
        first, last = date.fromisoformat(start), date.fromisoformat(end) - timedelta(days=1)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        return pd.DataFrame({"Close": [80.0 + (d - date(2020, 1, 1)).days * 0.001 for d in days]},
                            index=pd.to_datetime(days))


@pytest.fixture
def fx(monkeypatch):
    _FakeFx.calls = []
    monkeypatch.setattr(agent, "_FX_CACHE", {})
    monkeypatch.setattr(agent, "_FX_COVERED", [None, None], raising=False)
    monkeypatch.setattr(agent.yf, "Ticker", _FakeFx)
    return _FakeFx


def test_a_later_end_date_fetches_the_new_rates(fx):
    """The cache used to check only that the START date was held. A server that stayed up past
    midnight asked for the same start and a later end, found the start cached, and never fetched
    the newer rates - every dollar holding was converted at a stale rate for as long as the
    process lived."""
    s = date(2026, 1, 1)
    agent._usdinr_map(s, date(2026, 1, 10))
    out = agent._usdinr_map(s, date(2026, 1, 20))
    assert date(2026, 1, 20) in out
    assert len(fx.calls) == 2


def test_a_repeat_of_a_covered_window_does_not_refetch(fx):
    s, e = date(2026, 1, 1), date(2026, 1, 10)
    agent._usdinr_map(s, e)
    agent._usdinr_map(s, e)
    agent._usdinr_map(date(2026, 1, 3), date(2026, 1, 8))        # inside what is held
    assert len(fx.calls) == 1


def test_an_earlier_start_fetches_the_earlier_rates(fx):
    agent._usdinr_map(date(2026, 1, 10), date(2026, 1, 20))
    out = agent._usdinr_map(date(2025, 12, 1), date(2026, 1, 20))
    assert date(2025, 12, 1) in out


def test_a_failed_fetch_is_not_remembered_as_covered(fx, monkeypatch):
    def boom(self, start, end):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(_FakeFx, "history", boom)
    s, e = date(2026, 1, 1), date(2026, 1, 10)
    assert agent._usdinr_map(s, e) == {}
    monkeypatch.undo()
    monkeypatch.setattr(agent, "_FX_CACHE", {})
    monkeypatch.setattr(agent, "_FX_COVERED", [None, None], raising=False)
    monkeypatch.setattr(agent.yf, "Ticker", _FakeFx)
    assert date(2026, 1, 5) in agent._usdinr_map(s, e)           # the next call tries again


def test_two_separate_windows_leave_no_hole_between_them(fx):
    agent._usdinr_map(date(2026, 1, 1), date(2026, 1, 10))
    agent._usdinr_map(date(2026, 6, 1), date(2026, 6, 10))
    out = agent._usdinr_map(date(2026, 3, 1), date(2026, 3, 5))     # claimed covered
    assert date(2026, 3, 3) in out
