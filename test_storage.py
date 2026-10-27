"""Tests for saved portfolios.

The hermetic ones need no database: they cover the contract that matters most, which is
that EVERY path degrades to "off" rather than raising when storage is unreachable. The
live ones are -m regression and need SUPABASE_DB_URL, because ownership isolation is a
property of the SQL, and asserting it against a fake would only test the fake.
"""
import os

import pytest

import storage


# --- hermetic ------------------------------------------------------------------------------

def test_a_key_is_never_stored_only_its_hash():
    """A database dump must not hand anyone a working key."""
    k = storage.new_owner_key()
    h = storage.hash_key(k)
    assert h != k
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert storage.hash_key(k) == h, "hashing must be stable or rows become unreachable"
    assert storage.hash_key(storage.new_owner_key()) != h


def test_two_keys_are_never_the_same():
    assert len({storage.new_owner_key() for _ in range(500)}) == 500


def test_the_dsn_is_read_at_call_time_not_at_import(monkeypatch):
    """api.py does `import storage` BEFORE it calls load_dotenv(). A module-level
    `DSN = os.environ.get(...)` captured an empty string locally while working on Render,
    where the variables are already in the environment — correct in production, silently
    off on the machine where you would notice."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert storage.configured() is False
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@h:5432/d")
    assert storage.configured() is True, "a later env change must be picked up"


def test_every_call_degrades_quietly_when_storage_is_off(monkeypatch):
    """A paused Supabase project must cost the app its saving feature and nothing else.
    This mirrors logger.log_query, which swallows its own failures so that logging can
    never break a user's query."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert storage.save("k", "n", "csv") is None
    assert storage.list_for("k") == []
    assert storage.load("k", "id") is None
    assert storage.delete("k", "id") is False
    assert storage.init_db() is False


def test_an_unreachable_database_also_degrades_rather_than_raising(monkeypatch):
    """Configured but dead — a wrong host, a paused project, a dropped network. The
    difference from the test above is that these calls actually try to connect."""
    monkeypatch.setenv("SUPABASE_DB_URL",
                       "postgresql://u:p@127.0.0.1:1/doesnotexist")
    monkeypatch.setattr(storage, "CONNECT_TIMEOUT", 1)
    assert storage.configured() is True
    assert storage.save("k", "n", "csv") is None
    assert storage.list_for("k") == []
    assert storage.load("k", "id") is None
    assert storage.delete("k", "id") is False


def test_an_oversized_paste_is_refused_before_it_reaches_the_database(monkeypatch):
    """The cap is checked before connecting, so a 10MB paste cannot even open a socket."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    huge = "x" * (storage.MAX_CSV_BYTES + 1)
    assert storage.save("k", "n", huge) is None


def test_a_missing_key_is_refused_without_a_round_trip(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    assert storage.save("", "n", "csv") is None
    assert storage.list_for("") == []
    assert storage.load("", "id") is None
    assert storage.delete("", "id") is False


# --- live ----------------------------------------------------------------------------------

requires_db = pytest.mark.skipif(not os.environ.get("SUPABASE_DB_URL"),
                                 reason="SUPABASE_DB_URL not set")


@pytest.mark.regression
@requires_db
def test_one_browsers_portfolios_are_invisible_to_another():
    """The whole access model in one test. There are no accounts, so the only thing
    separating two visitors is that the owner hash is part of the WHERE clause — a wrong
    key finds no row, rather than finding one and being refused afterwards."""
    assert storage.init_db()
    alice, bob = storage.new_owner_key(), storage.new_owner_key()
    csv = open("portfolio.demo_b.csv", encoding="utf-8").read()
    pid = storage.save(alice, "Alice's book", csv)
    try:
        assert pid
        assert [r["name"] for r in storage.list_for(alice)] == ["Alice's book"]
        assert storage.list_for(bob) == [], "bob must not see alice's portfolio"
        assert storage.load(alice, pid)["csv_text"] == csv
        assert storage.load(bob, pid) is None, "bob must not read it"
        assert storage.delete(bob, pid) is False, "bob must not delete it"
    finally:
        storage.delete(alice, pid)
    assert storage.list_for(alice) == []


@pytest.mark.regression
@requires_db
def test_a_browser_cannot_fill_the_table():
    assert storage.init_db()
    k = storage.new_owner_key()
    ids = []
    try:
        for i in range(storage.MAX_PER_OWNER):
            pid = storage.save(k, f"p{i}", "a,b\n1,2\n")
            assert pid, f"save {i} should have succeeded"
            ids.append(pid)
        assert storage.save(k, "one too many", "a,b\n1,2\n") is None
    finally:
        for pid in ids:
            storage.delete(k, pid)
