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
    return os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL") or ""

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
