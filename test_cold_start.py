"""The first analysis after the server has been idle must not run into the proxy's time limit.

A first analysis of ten funds took ~115s on the live backend - past the ~100s the host's proxy
allows - so the user saw the proxy's raw HTML error page in a red box. The cost was downloading
and parsing the FULL NAV history of every candidate scheme (38 of them, ~5,000 rows each) just
to check a few purchase dates. These tests pin the fix: ask for only the window needed, never
show a web page as an error message, and warm the server early.

Hermetic: the NAV API is replaced.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import agent
import api
import uiformat
from xray_agent.adapters import fund_resolver as fr
from xray_agent.domain.transactions import BUY, Transaction


class FakeResponse:
    def __init__(self, rows, status=200):
        self._rows, self.status_code = rows, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return {"data": self._rows}


def rows(*pairs):
    return [{"date": d.strftime("%d-%m-%Y"), "nav": str(v)} for d, v in pairs]


@pytest.fixture(autouse=True)
def fresh_caches():
    agent._NAV_WINDOW.clear()
    agent._NAV_HISTORY.clear()
    yield
    agent._NAV_WINDOW.clear()
    agent._NAV_HISTORY.clear()


# --- asking for only the window -----------------------------------------------------------------

def test_only_the_requested_window_is_asked_for_and_parsed(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return FakeResponse(rows((date(2026, 3, 10), 100.5), (date(2026, 3, 11), 101.0),
                                 (date(2026, 4, 30), 999.0)))     # outside the window

    monkeypatch.setattr("requests.get", fake_get)
    out = agent.nav_window("120164", date(2026, 3, 1), date(2026, 3, 31))
    assert seen["params"] == {"startDate": "2026-03-01", "endDate": "2026-03-31"}
    assert seen["url"].endswith("/120164")
    assert out == {date(2026, 3, 10): 100.5, date(2026, 3, 11): 101.0}


def test_a_repeated_window_is_not_fetched_twice(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.get", lambda *a, **k: (calls.append(1),
                        FakeResponse(rows((date(2026, 3, 10), 100.0))))[1])
    agent.nav_window("1", date(2026, 3, 1), date(2026, 3, 31))
    agent.nav_window("1", date(2026, 3, 1), date(2026, 3, 31))
    assert len(calls) == 1


def test_if_the_ranged_request_fails_it_falls_back_to_the_full_history(monkeypatch):
    """A change in that API must degrade to the old, slow behaviour - not to no price at all."""
    def boom(*a, **k):
        raise RuntimeError("api changed")

    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr(agent, "nav_history",
                        lambda code: {date(2026, 3, 10): 100.0, date(2026, 6, 1): 120.0})
    assert agent.nav_window("1", date(2026, 3, 1), date(2026, 3, 31)) == {date(2026, 3, 10): 100.0}


def test_an_empty_answer_is_not_cached_so_a_hiccup_does_not_stick(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse([]))
    monkeypatch.setattr(agent, "nav_history", lambda code: {})
    assert agent.nav_window("1", date(2026, 3, 1), date(2026, 3, 31)) == {}
    assert agent._NAV_WINDOW == {}


def test_fund_prices_use_the_window_not_the_full_history(monkeypatch):
    got = {}
    monkeypatch.setattr(agent, "nav_window",
                        lambda code, start, end: got.update(a=(code, start, end)) or {start: 1.0})
    out = agent._price_map("mftool", "122639", date(2026, 1, 1), date(2026, 10, 1))
    assert got["a"] == ("122639", date(2026, 1, 1), date(2026, 10, 1)) and out


# --- the resolver asks for each holding's own purchase window -----------------------------------

def test_the_resolver_fetches_only_the_span_of_that_holdings_purchases():
    d0 = date(2026, 3, 10)
    nav = {d0 + timedelta(days=i): 100.0 + i for i in range(-10, 40)}
    txns = [Transaction(date=d0, side=BUY, units=1, price=100.0, name="Foo Fund Direct Growth"),
            Transaction(date=d0 + timedelta(days=20), side=BUY, units=1, price=120.0,
                        name="Foo Fund Direct Growth")]
    h = {"name": "Foo Fund Direct Growth", "symbol": "FOO", "source": "", "type": "",
         "market": "", "risk": "", "total_invested": 0.0, "inflows": [], "transactions": txns}
    windows = []

    def fake_window(code, lo, hi):
        windows.append((lo, hi))
        return {d: v for d, v in nav.items() if lo <= d <= hi}

    import agent as a
    orig = a.nav_window
    a.nav_window = fake_window
    try:
        notes = fr.resolve_holdings([h], master_fn=lambda: {"9": "Foo Fund"},
                                    detail_fn=lambda c: "")
    finally:
        a.nav_window = orig
    assert h["symbol"] == "9", notes
    assert windows and all(w == (d0 - timedelta(days=5), d0 + timedelta(days=25))
                           for w in windows), windows


# --- never show a web page as an error message --------------------------------------------------

HTML_ERROR = ("<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" "
              "lang=\"en-US\"> <![endif]-->\n<html><head><title>Gateway time-out</title>")


def test_a_proxy_error_page_becomes_a_readable_message():
    msg = uiformat.describe_http_failure(524, HTML_ERROR)
    assert msg == uiformat.SLOW_SERVER_NOTE
    assert "<" not in msg and "DOCTYPE" not in msg


def test_a_web_page_is_caught_even_with_an_unexpected_status():
    assert uiformat.describe_http_failure(200, HTML_ERROR.encode()) == uiformat.SLOW_SERVER_NOTE


@pytest.mark.parametrize("code", [502, 503, 504, 520, 522, 524])
def test_proxy_status_codes_with_any_body_are_treated_as_a_slow_server(code):
    assert uiformat.describe_http_failure(code, b"") == uiformat.SLOW_SERVER_NOTE


def test_an_ordinary_error_message_is_left_alone():
    assert uiformat.describe_http_failure(400, b'{"error": "bad file"}') is None
    assert uiformat.describe_http_failure(400, "Portfolio file not found.") is None


# --- the warm-up endpoint -----------------------------------------------------------------------

def test_warm_returns_at_once_and_starts_the_slow_load_only_once(monkeypatch):
    started = []
    monkeypatch.setattr(api, "_warming", False)
    monkeypatch.setattr(agent, "scheme_master", lambda: started.append(1) or {})
    c = TestClient(api.app)
    assert c.get("/warm").json() == {"warming": True}
    assert c.get("/warm").json() == {"warming": True}
    import time
    time.sleep(0.2)
    assert len(started) == 1, "a second call must not start a second download"
