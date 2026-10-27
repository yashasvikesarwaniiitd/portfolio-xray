"""One slow request must not stall the others.

Measured on the live backend: while a single analysis ran, a trivial /health took 15-20s. The
endpoints were `async def` doing blocking work inline, which stops the whole event loop - so a
second user, a cache hit and the host's own health check all waited behind one analysis.

These tests reproduce that with real concurrency (an async client driving the real app) rather
than asserting that the functions are `def`: a request is made slow, a trivial one is made while
it is in flight, and the trivial one must come back at once. They were written against the old
code first and fail there.

Hermetic: the slow work is a sleep standing in for market-data downloads.
"""
import asyncio
import json
import threading
import time

import httpx
import pytest

import agent
import api
from report import data as report_data

SLOW = 1.2          # how long the stand-in for a market-data download takes
FAST = 0.5          # a trivial request must beat this while the slow one is in flight

CSV = ("Date,Symbol,Stock name,Type,Quantity,Price,Order status\n"
       "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n")


def run(coro):
    return asyncio.run(coro)


def client(app=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or api.app), base_url="http://t")


async def stall_during(slow_call, app=None):
    """The longest time the event loop went without getting a turn while `slow_call` ran.

    A heartbeat ticks every 20ms. If the slow request blocks the loop, the heartbeat cannot tick
    and the gap shows it. (Timing a /health made after a fixed sleep would NOT: the loop is
    blocked during that sleep, so the sleep simply absorbs the stall and the health call that
    follows looks fast.) A real /health is also made while the slow call is in flight.
    """
    gaps, stop = [], asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    async with client(app) as c:
        beat = asyncio.create_task(heartbeat())
        slow = asyncio.create_task(slow_call(c))
        await asyncio.sleep(0)                        # let both start
        r = await c.get("/health")
        await slow
        stop.set()
        await beat
    assert r.status_code == 200
    return max(gaps)


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch):
    monkeypatch.setattr(api, "_bump", lambda *a, **k: True)
    api._analysis_cache.clear()
    yield
    api._analysis_cache.clear()


# --- the endpoints that blocked ---------------------------------------------------------------

def test_a_slow_analysis_does_not_stall_health(monkeypatch):
    monkeypatch.setattr(api, "_analysis_compute",
                        lambda *a, **k: (time.sleep(SLOW), {"ok": True})[1])

    async def slow(c):
        return await c.post("/analysis", files={"file": ("p.csv", CSV.encode(), "text/csv")})

    assert run(stall_during(slow)) < FAST


def test_a_slow_chat_does_not_stall_health(monkeypatch):
    monkeypatch.setattr(api, "client", lambda: object())
    monkeypatch.setattr(agent, "answer_query",
                        lambda *a, **k: (time.sleep(SLOW), {"answer": "x", "category": "education",
                                                            "refused": False,
                                                            "tools_used": []})[1])

    async def slow(c):
        return await c.post("/chat", json={"message": "What is XIRR?"})

    assert run(stall_during(slow)) < FAST


def test_a_slow_report_does_not_stall_health(monkeypatch):
    monkeypatch.setattr(api, "_report_compute",
                        lambda *a, **k: (time.sleep(SLOW), {"ok": True})[1])

    async def slow(c):
        return await c.post("/report", files={"file": ("p.csv", CSV.encode(), "text/csv")})

    assert run(stall_during(slow)) < FAST


def test_a_slow_database_save_does_not_stall_health(monkeypatch):
    """A save waits on a database in another region; that wait must not freeze the server."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(api.storage, "save", lambda *a, **k: (time.sleep(SLOW), "id1")[1])

    async def slow(c):
        return await c.post("/portfolios", json={"name": "n", "csv_text": CSV},
                            headers={"X-Owner-Key": "k"})

    assert run(stall_during(slow)) < FAST


def test_these_tests_would_actually_catch_the_old_behaviour():
    """Sanity check on the method: a blocking call inside `async def` DOES stall the loop here.
    Without this, a harness that cannot detect the problem would pass every test above."""
    from fastapi import FastAPI
    blocking = FastAPI()

    @blocking.get("/slow")
    async def slow():
        time.sleep(SLOW)
        return {}

    @blocking.get("/health")
    def health():
        return {}

    async def slow_call(c):
        return await c.get("/slow")

    assert run(stall_during(slow_call, blocking)) > FAST, (
        "the harness must be able to see the failure it guards against")


# --- the pool for heavy work ------------------------------------------------------------------

def test_a_cache_hit_never_waits_for_a_heavy_slot(monkeypatch):
    """Heavy work is limited to a few at once so a small host is not overwhelmed - but a cached
    answer takes no slot, so it must not queue behind analyses that do."""
    monkeypatch.setattr(api, "_HEAVY", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_analysis_compute",
                        lambda *a, **k: (time.sleep(SLOW), {"ok": True})[1])
    import hashlib
    cached = "upload:" + hashlib.sha256(CSV.encode()).hexdigest()
    api._cache_put(cached, {"from": "cache"})
    other_a, other_b = CSV + "x\n", CSV + "y\n"

    async def go():
        async with client() as c:
            a = asyncio.create_task(c.post("/analysis", files={"file": ("a.csv", other_a.encode(), "text/csv")}))
            b = asyncio.create_task(c.post("/analysis", files={"file": ("b.csv", other_b.encode(), "text/csv")}))
            await asyncio.sleep(0.25)
            t = time.perf_counter()
            r = await c.post("/analysis", files={"file": ("c.csv", CSV.encode(), "text/csv")})
            took = time.perf_counter() - t
            await asyncio.gather(a, b)
            return r, took

    r, took = run(go())
    assert r.json()["from"] == "cache" and took < FAST


def test_heavy_work_is_limited_so_a_small_host_is_not_overwhelmed(monkeypatch):
    """With one slot, two analyses take about twice as long as one: they queue rather than
    running together."""
    monkeypatch.setattr(api, "_HEAVY", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_analysis_compute",
                        lambda *a, **k: (time.sleep(0.6), {"ok": True})[1])

    async def go():
        async with client() as c:
            t = time.perf_counter()
            await asyncio.gather(*[c.post("/analysis", files={"file": (f"{i}.csv", (CSV + str(i)).encode(), "text/csv")})
                                   for i in range(2)])
            return time.perf_counter() - t

    assert run(go()) >= 1.1


# --- shared state that concurrency would otherwise corrupt ------------------------------------

def test_the_cache_file_is_never_seen_half_written(monkeypatch, tmp_path):
    """Every analysis rewrites this file and they now run at once. A plain open("w") truncates
    first, so a concurrent reader could see a fragment - corrupt JSON, silently treated as 'no
    cache'. An atomic replace means a reader sees a whole file or the other whole file."""
    path = tmp_path / "cache.json"
    monkeypatch.setattr(report_data, "CACHE_PATH", str(path))
    big = {f"sym{i}": {"sector": "x" * 200, "ts": "2026-01-01"} for i in range(400)}
    stop, bad = threading.Event(), []

    def writer():
        while not stop.is_set():
            report_data._save_cache(big)

    def reader():
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError):   # Windows: mid-replace
                continue
            try:
                json.loads(raw)
            except ValueError:
                bad.append(len(raw))

    threads = [threading.Thread(target=writer) for _ in range(4)] + \
              [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()
    assert bad == [], f"a reader saw {len(bad)} half-written file(s)"
    assert json.loads(path.read_text(encoding="utf-8")) == big


def test_the_nav_client_is_built_once_even_if_two_requests_ask_at_the_same_moment(monkeypatch):
    built = []

    class FakeMftool:
        def __init__(self):
            built.append(1)
            time.sleep(0.2)                        # the real constructor takes ~7s

    monkeypatch.setattr(agent, "Mftool", FakeMftool)
    monkeypatch.setattr(agent, "_MF_CLIENT", None)
    threads = [threading.Thread(target=agent._mf) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
