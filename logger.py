"""Lightweight append-only SQLite logging of per-query cost and latency.

One row per user query: timestamp, router category, tools called, models used, input/output
tokens (from the Anthropic response `usage` field), estimated cost, and wall-clock latency.
This is what lets us quote a real "cost per query / p95 latency" number later — start
measuring now. Logging never raises into the request path: a logging failure is swallowed.
"""
import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xray.db")

# Anthropic pricing, USD per MILLION tokens, from the claude-api reference (2026-10-27).
# Manually updated, like RISK_FREE_RATE.
#
# Haiku 5.5 has TWO rate cards chosen by prompt length: $0.10/$0.50 at or below 100K input
# tokens, $0.50/$2.50 above it. Everything here runs well under 100K (the longest measured
# investigation turn was ~13K), but modelling only the cheap card would silently
# under-report the day something long slips through, and an under-reported cost is a cost
# budget that does not stop a runaway.
#
# Cache reads bill at 0.1x the input rate and 5-minute cache writes at 1.25x, so a run that
# uses prompt caching cannot be priced from input_tokens alone.
PRICING = {
    "claude-haiku-5-5": {
        "input": 0.10, "output": 0.50,
        "long_input": 0.50, "long_output": 2.50, "long_threshold": 100_000,
    },
    # Kept so historical rows in query_log still price correctly after the migration.
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def price_call(model: str, input_tokens: int, output_tokens: int,
               cache_read: int = 0, cache_write: int = 0) -> float:
    """USD for one model call. Unknown model -> 0.0, which is why a new model must be added
    here deliberately: an unpriced model is invisible to the cost budget."""
    p = PRICING.get(model)
    if not p:
        return 0.0
    billed_input = input_tokens + cache_read + cache_write
    long = "long_threshold" in p and billed_input > p["long_threshold"]
    in_rate = (p["long_input"] if long else p["input"]) / 1_000_000
    out_rate = (p["long_output"] if long else p["output"]) / 1_000_000
    return (input_tokens * in_rate
            + cache_read * in_rate * CACHE_READ_MULTIPLIER
            + cache_write * in_rate * CACHE_WRITE_MULTIPLIER
            + output_tokens * out_rate)
USD_TO_INR = 86.0  # approximate; manually updated for the ₹-per-query figure


class Usage:
    """Accumulates per-model token usage across the several model calls a single query makes
    (router + agent turns + judges). Pass one instance through a query, then .cost()/.summary()."""

    def __init__(self):
        self.calls = []  # list of (model, input_tokens, output_tokens)

    def add(self, model: str, resp) -> None:
        """Record one call. Cache tokens are captured separately because they bill at a
        different rate — counting a cached read as full-price input would overstate the
        cost of exactly the thing we added caching to make cheap."""
        u = getattr(resp, "usage", None)
        if u is None:
            return
        self.calls.append((
            model,
            int(getattr(u, "input_tokens", 0) or 0),
            int(getattr(u, "output_tokens", 0) or 0),
            int(getattr(u, "cache_read_input_tokens", 0) or 0),
            int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        ))

    def totals(self) -> tuple[int, int]:
        """(input, output). Input INCLUDES cache reads and writes, so the logged token
        count still reflects everything that was actually sent."""
        return (sum(c[1] + c[3] + c[4] for c in self.calls),
                sum(c[2] for c in self.calls))

    def cache_totals(self) -> tuple[int, int]:
        """(read, written). Read staying at 0 across repeated calls means caching is not
        working — the most common cause being a prefix shorter than the model's minimum."""
        return (sum(c[3] for c in self.calls), sum(c[4] for c in self.calls))

    def models(self) -> list[str]:
        seen = []
        for c in self.calls:
            if c[0] not in seen:
                seen.append(c[0])
        return seen

    def cost_usd(self) -> float:
        return round(sum(price_call(*c) for c in self.calls), 6)


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                category TEXT,
                tools TEXT,
                models TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                est_cost_usd REAL,
                latency_ms INTEGER
            )
        """)


def log_query(ts: str, category: str, tools: list, usage: Usage,
              latency_ms: int, path: str = DB_PATH) -> None:
    """Append one row. `ts` is passed in (caller stamps the time) so this stays pure of clocks.
    Swallows all errors — logging must never break a user's query."""
    try:
        init_db(path)
        inp, out = usage.totals()
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO query_log (ts, category, tools, models, input_tokens, "
                "output_tokens, est_cost_usd, latency_ms) VALUES (?,?,?,?,?,?,?,?)",
                (ts, category, json.dumps(tools), json.dumps(usage.models()),
                 inp, out, usage.cost_usd(), latency_ms),
            )
    except Exception:
        pass  # never propagate a logging failure into the request path


def summarize(path: str = DB_PATH) -> dict:
    """Aggregate stats for a quick 'cost per query / latency' readout."""
    if not os.path.exists(path):
        return {"queries": 0}
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), AVG(est_cost_usd), AVG(latency_ms), MAX(latency_ms), "
            "SUM(est_cost_usd) FROM query_log"
        ).fetchone()
    n = row[0] or 0
    if not n:
        return {"queries": 0}
    return {
        "queries": n,
        "avg_cost_usd": round(row[1] or 0, 6),
        "avg_cost_inr": round((row[1] or 0) * USD_TO_INR, 4),
        "avg_latency_ms": round(row[2] or 0),
        "max_latency_ms": row[3] or 0,
        "total_cost_usd": round(row[4] or 0, 6),
    }
