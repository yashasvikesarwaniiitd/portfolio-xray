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


def test_update_degrades_quietly_like_every_other_call(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert storage.update("k", "id", name="n") is False
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/doesnotexist")
    monkeypatch.setattr(storage, "CONNECT_TIMEOUT", 1)
    assert storage.update("k", "id", name="n") is False


def test_update_refuses_nothing_to_change_and_oversized_or_empty_contents(monkeypatch):
    """Checked before connecting, so none of these can even open a socket."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    assert storage.update("k", "id") is False
    assert storage.update("k", "id", csv_text="   ") is False
    assert storage.update("k", "id", csv_text="x" * (storage.MAX_CSV_BYTES + 1)) is False
    assert storage.update("", "id", name="n") is False


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


NL = chr(10)


@pytest.mark.regression
@requires_db
def test_one_browser_cannot_update_anothers_portfolio():
    """The ownership property for the UPDATE path, which is a different SQL statement from
    load and delete and so needs its own proof: the owner hash is in its WHERE clause."""
    assert storage.init_db()
    alice, bob = storage.new_owner_key(), storage.new_owner_key()
    first, second, hijack = "a,b" + NL + "1,2" + NL, "c,d" + NL + "3,4" + NL, "x,y" + NL + "9,9" + NL
    pid = storage.save(alice, "Alice's book", first)
    try:
        assert pid
        assert storage.update(bob, pid, name="hijacked", csv_text=hijack) is False
        got = storage.load(alice, pid)
        assert got["name"] == "Alice's book" and got["csv_text"] == first
        assert storage.update(alice, pid, name="Renamed", csv_text=second) is True
        got = storage.load(alice, pid)
        assert got["name"] == "Renamed" and got["csv_text"] == second
        assert [r["id"] for r in storage.list_for(alice)] == [pid], "updated in place"
    finally:
        storage.delete(alice, pid)


def test_diagnose_names_the_kind_of_failure_without_leaking_the_message(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert storage.diagnose() == {"reachable": False, "problem": "not_configured"}
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:secretpw@127.0.0.1:1/doesnotexist")
    monkeypatch.setattr(storage, "CONNECT_TIMEOUT", 1)
    d = storage.diagnose()
    assert d["reachable"] is False and d["problem"]
    assert "secretpw" not in str(d) and "127.0.0.1" not in str(d)


@pytest.mark.regression
@requires_db
def test_diagnose_reports_a_working_database_as_reachable():
    assert storage.init_db()
    assert storage.diagnose() == {"reachable": True, "problem": None}


# --- a password pasted without URL-encoding ----------------------------------------------------

HOST = "aws-0-ap-southeast-1.pooler.supabase.com"


def parsed(url):
    """What the DRIVER makes of a string - the only judge that matters."""
    from psycopg.conninfo import conninfo_to_dict
    return conninfo_to_dict(storage.normalise_dsn(url))


@pytest.mark.parametrize("password", ["plain123", "Pass@word", "a/b#c?d", "p@ss@w0rd@x", "co:lon"])
def test_a_password_with_reserved_characters_still_reaches_the_right_host(password):
    """The live failure: the first @ ended the userinfo early, the driver read the tail of the
    password plus the real host as the HOST, and reported "could not translate host name" -
    which looks like a network fault and is not."""
    d = parsed(f"postgresql://postgres.abcdef:{password}@{HOST}:5432/postgres")
    assert d["host"] == HOST and d["port"] == "5432" and d["dbname"] == "postgres"
    assert d["user"] == "postgres.abcdef"
    assert d["password"] == password, "the password must arrive exactly as typed"


def test_the_unfixed_string_really_does_fail_so_this_test_is_not_vacuous():
    from psycopg.conninfo import conninfo_to_dict
    d = conninfo_to_dict(f"postgresql://postgres.abcdef:Pass@word@{HOST}:5432/postgres")
    assert d["host"] != HOST, "without normalising, the host is misread"


def test_a_correctly_encoded_string_comes_out_unchanged_and_is_never_double_encoded():
    good = f"postgresql://postgres.abcdef:Pass%40word@{HOST}:5432/postgres"
    assert storage.normalise_dsn(good) == good
    assert parsed(good)["password"] == "Pass@word"
    assert storage.normalise_dsn(storage.normalise_dsn(good)) == good


def test_strings_it_does_not_understand_are_returned_untouched():
    for odd in ("", "not a url", "postgresql://hostonly/db", f"postgresql://user@{HOST}/db",
                "mysql://u:p@h/d"):
        assert storage.normalise_dsn(odd) == odd


def test_the_connection_uses_the_normalised_string(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", f"postgresql://u:Pa@ss@{HOST}:5432/postgres")
    assert storage._dsn() == f"postgresql://u:Pa%40ss@{HOST}:5432/postgres"
