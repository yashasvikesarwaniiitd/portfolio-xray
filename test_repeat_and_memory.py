"""The cold wait: remember fund matches, don't price twice, cache repeats, and be honest while
waiting.

A first analysis after the server slept took ~44s (after an earlier fix from 115s), on top of the
~30-50s the free-tier host needs to wake. Most of it was repeated work: every restart forgot
which scheme each fund had been verified against, every holding was priced twice, and every
refresh or re-open ran the whole analysis again. These tests pin each remedy.

Hermetic: the NAV API, the market data and the database are all replaced.
"""
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import agent
import api
import storage
import uiformat
from report import data as report_data
from xray_agent.adapters import fund_resolver as fr
from xray_agent.domain.transactions import BUY, Transaction

D0 = date(2026, 3, 10)
NAV = {D0 + timedelta(days=i): 100.0 + i for i in range(-10, 40)}


def holding(name="Foo Fund Direct Growth"):
    txns = [Transaction(date=D0, side=BUY, units=1, price=100.0, name=name),
            Transaction(date=D0 + timedelta(days=20), side=BUY, units=1, price=120.0, name=name)]
    return {"name": name, "symbol": name.upper(), "source": "", "type": "", "market": "",
            "risk": "", "total_invested": 0.0, "inflows": [], "transactions": txns}


class FakeStore:
    def __init__(self, known=None):
        self.known, self.put_calls, self.get_calls = dict(known or {}), [], []

    def get(self, keys):
        self.get_calls.append(list(keys))
        return {k: v for k, v in self.known.items() if k in keys}

    def put(self, items):
        self.put_calls.append(list(items))


@pytest.fixture(autouse=True)
def clean_memory():
    fr._MATCH_MEMORY.clear()
    yield
    fr._MATCH_MEMORY.clear()


def resolve(h, store, master, fetched=None):
    def nav_fn(code):
        (fetched if fetched is not None else []).append(code)
        return NAV if code == "9" else {d: v * 0.5 for d, v in NAV.items()}   # "9" is the right one
    master_calls = []

    def master_fn():
        master_calls.append(1)
        return master

    notes = fr.resolve_holdings([h], master_fn=master_fn, nav_fn=nav_fn,
                                detail_fn=lambda c: "", match_store=store)
    return notes, master_calls


# --- remembering fund matches -----------------------------------------------------------------

def test_a_remembered_match_is_reverified_alone_and_the_fund_list_is_never_loaded():
    h = holding()
    key = fr.match_key(h["name"])
    store = FakeStore({key: {"code": "9", "scheme_name": "Foo Fund"}})
    fetched = []
    notes, master_calls = resolve(h, store, {"9": "Foo Fund", "10": "Foo Fund", "11": "Foo Fund"},
                                  fetched)
    assert h["symbol"] == "9" and h["source"] == "mftool"
    assert master_calls == [], "the 14,000-scheme list must not be loaded when nothing needs it"
    assert set(fetched) == {"9"}, "only the remembered scheme is checked, not every candidate"
    assert "verified" in notes[0]


def test_a_new_match_is_verified_against_all_candidates_and_then_remembered():
    h = holding()
    store = FakeStore()
    fetched = []
    resolve(h, store, {"9": "Foo Fund", "10": "Foo Fund", "11": "Foo Fund"}, fetched)
    assert h["symbol"] == "9"
    assert {"9", "10", "11"} <= set(fetched)
    time.sleep(0.2)                                  # the write happens in the background
    assert store.put_calls and store.put_calls[0][0][1] == "9"
    assert store.put_calls[0][0][0] == fr.match_key(h["name"])


def test_a_remembered_match_that_no_longer_fits_the_statement_is_not_trusted():
    """The memory can only make this faster. If the remembered scheme's NAVs stop matching, the
    full search runs and finds the right one - a wrong match must never stick."""
    h = holding()
    key = fr.match_key(h["name"])
    store = FakeStore({key: {"code": "10", "scheme_name": "Foo Fund"}})   # "10" does not match
    notes, master_calls = resolve(h, store, {"9": "Foo Fund", "10": "Foo Fund"})
    assert h["symbol"] == "9", notes
    assert master_calls == [1], "it must fall back to the full search"


def test_the_second_request_in_the_same_process_does_not_even_ask_the_database():
    h1, h2 = holding(), holding()
    store = FakeStore()
    resolve(h1, store, {"9": "Foo Fund"})
    store.get_calls.clear()
    resolve(h2, store, {"9": "Foo Fund"})
    assert store.get_calls == [], "this process already knows it"


def test_a_test_that_only_injects_a_fund_list_never_touches_the_memory_or_a_database():
    """Otherwise one test's remembered match would leak into the next test's."""
    h = holding()
    fr.resolve_holdings([h], master_fn=lambda: {"9": "Foo Fund"},
                        nav_fn=lambda c: NAV, detail_fn=lambda c: "")
    assert fr._MATCH_MEMORY == {}


def test_the_match_key_separates_direct_from_regular():
    """They are different schemes with different codes."""
    assert fr.match_key("Foo Fund Direct Growth") != fr.match_key("Foo Fund Regular Growth")
    assert fr.match_key("Foo  Fund - Direct Growth") == fr.match_key("foo fund direct growth")


# --- the storage layer ------------------------------------------------------------------------

def test_fund_match_storage_degrades_quietly_when_the_database_is_off():
    assert storage.fund_matches_get(["a"]) == {}
    assert storage.fund_matches_put([("a", "123", "Some Fund")]) is False


def test_only_well_formed_matches_can_be_stored(monkeypatch):
    """The table is keyed by public scheme names and AMFI codes; it must not become a place to
    park arbitrary text. Rejected before connecting, so no socket is opened."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    assert storage.fund_matches_put([("k", "not-a-code", "Fund")]) is False
    assert storage.fund_matches_put([("k" * 500, "123", "Fund")]) is False
    assert storage.fund_matches_put([("", "123", "Fund")]) is False
    assert storage.fund_matches_put([("k", "123", "n" * 500)]) is False


@pytest.mark.regression
def test_fund_matches_round_trip_in_the_real_database(monkeypatch):
    import os
    if not os.environ.get("SUPABASE_DB_URL"):
        pytest.skip("SUPABASE_DB_URL not set")
    key = "zz-test-" + storage.new_owner_key()[:12]
    try:
        assert storage.fund_matches_put([(key, "123456", "Test Fund")])
        assert storage.fund_matches_get([key]) == {
            key: {"code": "123456", "scheme_name": "Test Fund"}}
        assert storage.fund_matches_put([(key, "654321", "Renamed")])     # upsert, not a duplicate
        assert storage.fund_matches_get([key])[key]["code"] == "654321"
    finally:
        import psycopg
        with psycopg.connect(storage._dsn(), connect_timeout=8) as c:
            c.execute("DELETE FROM fund_match WHERE name_key = %s", (key,))


# --- one valuation per holding ---------------------------------------------------------------

def test_weights_use_the_valuations_the_caller_already_has(monkeypatch):
    monkeypatch.setattr(agent, "reconstruct_holding",
                        lambda *a, **k: pytest.fail("a holding was priced a second time"))
    holdings = [{"name": "A", "symbol": "A", "source": "manual", "type": "Share",
                 "market": "India", "risk": "", "total_invested": 100.0},
                {"name": "B", "symbol": "B", "source": "manual", "type": "Share",
                 "market": "India", "risk": "", "total_invested": 300.0}]
    valued = [{"status": "priced", "current_value": 100.0},
              {"status": "priced", "current_value": 300.0}]
    rows = report_data.holdings_with_weights(holdings, "L2", valued=valued)
    assert [r["weight_pct"] for r in rows] == [25.0, 75.0]


def test_a_misaligned_valuation_list_is_ignored_rather_than_weighting_the_wrong_holdings(
        monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "reconstruct_holding",
                        lambda h, *a, **k: calls.append(h["name"]) or {"status": "unavailable"})
    holdings = [{"name": "A", "symbol": "A", "source": "manual", "type": "Share",
                 "market": "India", "risk": "", "total_invested": 100.0}]
    report_data.holdings_with_weights(holdings, "L2", valued=[])
    assert calls == ["A"]


# --- the repeat-analysis cache ---------------------------------------------------------------

CSV = ("Mode,App,Type,Market,Risk,Where,symbol,source,Estimated Returns (3Y),Total Invested,"
       "Mar'25\nEquity,Groww,Basket,India,Med,Thing,,manual,-,1000,1000\n")
CSV2 = CSV.replace("Thing", "Other thing")


@pytest.fixture
def pipeline(monkeypatch):
    """Run the real /analysis with the market-data edges replaced, counting how often the file
    is actually read - that is how a cache hit shows."""
    reads = []
    real_read = agent.read_portfolio

    def counting_read(*a, **k):
        # Every request also reads the file once, cheaply, to validate it
        # (resolve_funds=False). Only the real analysis read counts as "the analysis ran".
        if k.get("resolve_funds", True):
            reads.append(1)
        return real_read(*a, **k)

    monkeypatch.setattr(agent, "read_portfolio", counting_read)
    monkeypatch.setattr(api, "_nifty_window_return", lambda *a, **k: {"symbol": "NIFTY 50"})
    monkeypatch.setattr(report_data, "_save_cache", lambda c: None)
    monkeypatch.setattr(report_data, "_load_cache", lambda: {})
    api._analysis_cache.clear()
    yield reads
    api._analysis_cache.clear()


def post(client, text):
    return client.post("/analysis", files={"file": ("p.csv", text.encode(), "text/csv")})


def test_an_identical_upload_is_answered_from_the_cache(pipeline):
    c = TestClient(api.app)
    first, second = post(c, CSV), post(c, CSV)
    assert first.status_code == second.status_code == 200
    assert len(pipeline) == 1, "the second request must not re-run the analysis"
    assert "cache_age_s" not in first.json()
    assert second.json()["cache_age_s"] >= 0
    assert {k: v for k, v in second.json().items() if k != "cache_age_s"} == first.json()


def test_a_different_file_is_not_served_from_the_cache(pipeline):
    c = TestClient(api.app)
    post(c, CSV)
    post(c, CSV2)
    assert len(pipeline) == 2


def test_the_cache_key_is_a_hash_and_never_the_contents():
    api._analysis_cache.clear()
    api._cache_put("upload:" + "a" * 64, {"x": 1})
    assert all("Thing" not in k and "Mar'25" not in k for k in api._analysis_cache)


def test_a_result_older_than_the_ttl_is_never_served(monkeypatch):
    api._analysis_cache.clear()
    api._cache_put("k", {"x": 1})
    stored_at, result = api._analysis_cache["k"]
    api._analysis_cache["k"] = (stored_at - api.ANALYSIS_CACHE_TTL - 1, result)
    assert api._cache_get("k") is None
    assert "k" not in api._analysis_cache, "an expired entry is dropped, not kept around"


def test_the_cache_is_bounded(monkeypatch):
    api._analysis_cache.clear()
    for i in range(api.ANALYSIS_CACHE_MAX + 5):
        api._cache_put(f"k{i}", {"i": i})
    assert len(api._analysis_cache) == api.ANALYSIS_CACHE_MAX
    assert api._cache_get("k0") is None, "the oldest entry is the one evicted"
    assert api._cache_get(f"k{api.ANALYSIS_CACHE_MAX + 4}") is not None


def test_an_incomplete_result_is_not_cached_so_a_retry_can_fix_it(pipeline, monkeypatch):
    """A market reference that came back empty must not be served for ten minutes."""
    monkeypatch.setattr(api, "_nifty_window_return", lambda *a, **k: None)
    c = TestClient(api.app)
    post(c, CSV)
    post(c, CSV)
    assert len(pipeline) == 2, "both requests did the work: nothing was cached"
    assert api._analysis_cache == {}


# --- an honest wait ---------------------------------------------------------------------------

def test_the_first_message_names_what_is_actually_happening():
    assert "sample" in uiformat.progress_message(0, "sample").lower()
    assert "saved" in uiformat.progress_message(0, "saved").lower()
    assert "file" in uiformat.progress_message(0, "file").lower()


def test_it_no_longer_claims_to_reconstruct_a_sip_history():
    """That wording was shown for a broker statement, the sample and a saved portfolio, none of
    which have one."""
    for elapsed in (0, 5, 30, 90):
        for kind in ("file", "sample", "saved"):
            assert "SIP" not in uiformat.progress_message(elapsed, kind)


def test_a_long_wait_says_why_and_that_later_runs_are_quick():
    assert "waking" in uiformat.progress_message(8).lower()
    mid = uiformat.progress_message(30).lower()
    assert "first run" in mid and "quick" in mid
    assert "try again" in uiformat.progress_message(90).lower()


def test_the_message_changes_as_the_wait_goes_on():
    msgs = [uiformat.progress_message(t) for t in (0, 8, 30, 90)]
    assert len(set(msgs)) == 4
