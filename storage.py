"""Saved portfolios, in Postgres — the one place this product keeps user data.

Everything else here is deliberately transient: an uploaded CSV lives in a temp file for
one request and is deleted in a `finally`, and the query log carries tool names and token
counts but never a holding or an amount. This module is the exception, and it exists
because "your portfolios vanish when you refresh" was the single thing people hit first.

WHO OWNS A ROW, WITH NO ACCOUNTS. There is no login, so there is no user id to key on.
Instead the browser holds a random secret and the server stores only its SHA-256. A row is
reachable by whoever presents the key and by nobody else — not by another visitor, not by
enumeration, and not by anyone reading a database dump, because the dump holds hashes. The
server never issues or records the key itself, which also means it cannot help you recover
one. That is the trade: no password to steal, and no password reset either.

WHAT IS STORED. The raw CSV, which is what the user uploaded, and nothing derived. The
analysis is NOT stored: it is a function of today's prices, so a saved copy would be a
wrong answer dressed as a saved one, and re-running it on load costs a few seconds and
tells the truth. That also keeps rows at roughly a kilobyte.

FAILURE IS NOT AN ERROR. Every public function returns a falsy sentinel instead of raising
if the database is unreachable, so a paused project or a dropped connection degrades the
app to exactly its old behaviour — portfolios held in session, lost on refresh — rather
than breaking a page. This mirrors logger.log_query, which swallows its own failures so
that logging can never break a user's query.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

def _dsn() -> str:
    """Read at CALL time, never at import.

    api.py does `import storage` before it calls load_dotenv(), so a module-level
    `DSN = os.environ.get(...)` captured an empty string locally while working fine on
    Render, where the variables are already in the environment. That is the worst shape a
    configuration bug can take: correct in production, silently off on the machine where
    you would notice.
    """
    return normalise_dsn(os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
                         or "")


def normalise_dsn(url: str) -> str:
    """Make a connection string survive a password that was pasted without URL-encoding.

    A URL's password may not contain `@`, `/`, `#`, `?` or `:` unescaped, but people paste the
    string Supabase shows with the password typed straight in. The FIRST `@` then ends the
    userinfo early, so the driver reads the rest of the password plus the real host as the host
    name and fails with "could not translate host name" - which looks like a network problem
    and is not. Measured earlier in this project: `...:Pass@word@aws-0-....pooler...` failed
    with `failed to resolve host 'word@aws-0-...'`.

    The real host is everything after the LAST `@`, so split there, and percent-encode the
    password. Already-encoded input is decoded first and re-encoded, so a correct string comes
    out unchanged and nothing is ever double-encoded.
    """
    from urllib.parse import quote, unquote
    for scheme in ("postgresql://", "postgres://"):
        if not url.startswith(scheme):
            continue
        rest = url[len(scheme):]
        at = rest.rfind("@")
        if at < 0:
            return url
        userinfo, hostpart = rest[:at], rest[at + 1:]
        user, colon, password = userinfo.partition(":")
        if not colon:
            return url
        return f"{scheme}{quote(unquote(user), safe='.')}:{quote(unquote(password), safe='')}@{hostpart}"
    return url

# A portfolio is ~1KB of CSV. The cap is three orders of magnitude above that: big enough
# for a long transaction history, small enough that one paste cannot fill the table.
MAX_CSV_BYTES = 256_000
# Per key, so one visitor cannot grow the table without bound.
MAX_PER_OWNER = 12
# Rows past this are deleted on the next write by whoever touches the table, the same way
# api._bump prunes the rate-limit table rather than accumulating rows forever.
TTL_DAYS = 90
CONNECT_TIMEOUT = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_portfolio (
    id              TEXT PRIMARY KEY,
    owner_key_hash  TEXT NOT NULL,
    name            TEXT NOT NULL,
    csv_text        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_portfolio_owner  ON saved_portfolio (owner_key_hash);
CREATE INDEX IF NOT EXISTS ix_saved_portfolio_expiry ON saved_portfolio (expires_at);
"""


def configured() -> bool:
    """Is storage switched on at all? False keeps the app on its session-only behaviour."""
    return bool(_dsn())


def hash_key(owner_key: str) -> str:
    return hashlib.sha256((owner_key or "").encode("utf-8")).hexdigest()


def new_owner_key() -> str:
    """128 bits from the OS. Not a password: it is never typed, shown once, and the server
    only ever sees its hash."""
    return uuid.uuid4().hex


def _connect():
    import psycopg  # imported here so the app runs with storage switched off
    return psycopg.connect(_dsn(), connect_timeout=CONNECT_TIMEOUT)


def init_db() -> bool:
    if not configured():
        return False
    try:
        with _connect() as conn:
            conn.execute(SCHEMA)
        return True
    except Exception:
        return False


def diagnose() -> dict:
    """Is the database actually reachable, and if not, what KIND of problem is it?

    Every other function here swallows its errors on purpose, so a bad connection string
    looks exactly like "no portfolios saved yet" - the app reports storage as ON (the
    variable is set) while quietly saving nothing. That is the worst failure to debug from
    outside. This returns a coarse category and never the message itself: a driver error can
    contain the host or user name, and this is served to anyone who asks, so only a fixed
    label leaves the process. The password is never in these messages and never printed.
    """
    if not configured():
        return {"reachable": False, "problem": "not_configured"}
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
            has_table = conn.execute(
                "SELECT to_regclass('public.saved_portfolio') IS NOT NULL").fetchone()[0]
        return {"reachable": True, "problem": None if has_table else "table_missing"}
    except Exception as e:
        msg = f"{type(e).__name__} {e}".lower()
        for needle, label in (
                ("password authentication", "bad_password"),
                ("tenant or user not found", "wrong_user_or_project"),
                ("translate host", "unknown_host"), ("resolve", "unknown_host"),
                ("name or service", "unknown_host"),
                ("network is unreachable", "ipv6_only_host_use_session_pooler"),
                ("timeout", "timeout"), ("timed out", "timeout"),
                ("ssl", "ssl"), ("invalid", "malformed_connection_string"),
                ("missing", "malformed_connection_string")):
            if needle in msg:
                return {"reachable": False, "problem": label}
        return {"reachable": False, "problem": "other:" + type(e).__name__}


def _prune(conn) -> None:
    conn.execute("DELETE FROM saved_portfolio WHERE expires_at < now()")


def save(owner_key: str, name: str, csv_text: str) -> str | None:
    """Returns the new row's id, or None if it could not be stored for any reason.

    A name is not unique: saving "Groww" twice keeps both, because silently replacing
    someone's earlier portfolio to save a row is a worse outcome than two rows.
    """
    if not (configured() and owner_key and csv_text):
        return None
    if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
        return None
    pid, h = uuid.uuid4().hex, hash_key(owner_key)
    expires = datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)
    try:
        with _connect() as conn:
            _prune(conn)
            n = conn.execute(
                "SELECT count(*) FROM saved_portfolio WHERE owner_key_hash = %s", (h,)
            ).fetchone()[0]
            if n >= MAX_PER_OWNER:
                return None
            conn.execute(
                "INSERT INTO saved_portfolio (id, owner_key_hash, name, csv_text, "
                "expires_at) VALUES (%s, %s, %s, %s, %s)",
                (pid, h, (name or "Portfolio")[:80], csv_text, expires))
        return pid
    except Exception:
        return None


def list_for(owner_key: str) -> list[dict]:
    """Metadata only — never the CSV, so listing cannot become a bulk export."""
    if not (configured() and owner_key):
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, last_opened_at FROM saved_portfolio "
                "WHERE owner_key_hash = %s AND expires_at > now() "
                "ORDER BY last_opened_at DESC", (hash_key(owner_key),)).fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2].isoformat(),
                 "last_opened_at": r[3].isoformat()} for r in rows]
    except Exception:
        return []


def load(owner_key: str, pid: str) -> dict | None:
    """The owner hash is part of the WHERE clause, not checked after fetching: a wrong key
    finds no row rather than finding one and being refused."""
    if not (configured() and owner_key and pid):
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, name, csv_text FROM saved_portfolio "
                "WHERE id = %s AND owner_key_hash = %s AND expires_at > now()",
                (pid, hash_key(owner_key))).fetchone()
            if not row:
                return None
            conn.execute("UPDATE saved_portfolio SET last_opened_at = now() "
                         "WHERE id = %s", (pid,))
        return {"id": row[0], "name": row[1], "csv_text": row[2]}
    except Exception:
        return None


def update(owner_key: str, pid: str, name: str | None = None,
           csv_text: str | None = None) -> bool:
    """Change a saved portfolio in place - its contents, its name, or both.

    This is what "add to my portfolio" and "rename" need. Without it the only way to extend
    a saved portfolio was to save a new row and delete the old one, which lands the portfolio
    at the back of the list with a new id and costs a second round trip to another continent.

    The owner hash is in the WHERE clause, as everywhere else: a wrong key updates nothing
    and reports False. Expiry restarts on every update, because a portfolio someone is
    still adding to is plainly not abandoned.
    """
    if not (configured() and owner_key and pid) or (name is None and csv_text is None):
        return False
    if csv_text is not None:
        if not csv_text.strip() or len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
            return False
    sets, args = [], []
    if name is not None:
        sets.append("name = %s")
        args.append((name or "Portfolio")[:80])
    if csv_text is not None:
        sets.append("csv_text = %s")
        args.append(csv_text)
    sets += ["last_opened_at = now()", "expires_at = %s"]
    args.append(datetime.now(timezone.utc) + timedelta(days=TTL_DAYS))
    try:
        with _connect() as conn:
            cur = conn.execute(
                f"UPDATE saved_portfolio SET {', '.join(sets)} "
                "WHERE id = %s AND owner_key_hash = %s AND expires_at > now()",
                (*args, pid, hash_key(owner_key)))
            return cur.rowcount > 0
    except Exception:
        return False


# --- verified fund matches ---------------------------------------------------------------------
#
# A fund on a statement is matched to an AMFI scheme code by checking candidate schemes' NAVs
# against the NAVs the statement recorded (see fund_resolver). On a free-tier host the process
# is thrown away after ~15 minutes idle, and with it the in-memory record of those matches, so
# every first request after a sleep re-verified up to 38 candidate schemes: ~44s, on top of
# the ~30-50s the host takes to wake. A verified match is stable - "Kotak Small Cap Fund Direct
# Growth" does not change scheme - so it is kept here, and on a later request only the ONE
# remembered scheme is re-checked, not all the candidates.
#
# This table holds PUBLIC data only: a scheme name as printed on a statement and the AMFI code
# it resolved to. No amounts, dates, units or owner key. It is keyed by name, not by user, so
# one person's verification saves the next person's.

FUND_MATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_match (
    name_key     TEXT PRIMARY KEY,
    code         TEXT NOT NULL,
    scheme_name  TEXT NOT NULL,
    verified_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
_fund_table_ready = False


def _ensure_fund_table(conn) -> None:
    """Create the table on first use in this process, so no deploy step has to remember to."""
    global _fund_table_ready
    if not _fund_table_ready:
        conn.execute(FUND_MATCH_SCHEMA)
        _fund_table_ready = True


def fund_matches_get(keys: list) -> dict:
    """{name_key: {"code", "scheme_name"}} for the keys that have a remembered match.
    Empty on any failure: a missing memory only costs the slower path it replaces."""
    keys = [k for k in keys if k]
    if not (configured() and keys):
        return {}
    try:
        with _connect() as conn:
            _ensure_fund_table(conn)
            rows = conn.execute(
                "SELECT name_key, code, scheme_name FROM fund_match WHERE name_key = ANY(%s)",
                (keys,)).fetchall()
        return {r[0]: {"code": r[1], "scheme_name": r[2]} for r in rows}
    except Exception:
        return {}


def fund_matches_put(items: list) -> bool:
    """Remember [(name_key, code, scheme_name), ...]. Only well-formed entries are kept: the
    code must be digits (an AMFI scheme code) and the key a sane length, so this cannot be
    used to fill the table with arbitrary text."""
    good = [(k, c, n) for k, c, n in items
            if k and len(k) <= 200 and str(c).isdigit() and n and len(n) <= 300]
    if not (configured() and good):
        return False
    try:
        with _connect() as conn:
            _ensure_fund_table(conn)
            for k, c, n in good:
                conn.execute(
                    "INSERT INTO fund_match (name_key, code, scheme_name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name_key) DO UPDATE SET code = EXCLUDED.code, "
                    "scheme_name = EXCLUDED.scheme_name, verified_at = now()",
                    (k, str(c), n))
        return True
    except Exception:
        return False


def delete(owner_key: str, pid: str) -> bool:
    if not (configured() and owner_key and pid):
        return False
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM saved_portfolio WHERE id = %s AND owner_key_hash = %s",
                (pid, hash_key(owner_key)))
            return cur.rowcount > 0
    except Exception:
        return False
