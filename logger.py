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

# Anthropic pricing, USD per token. Haiku 4.5 = $1.00 / 1M input, $5.00 / 1M output
# (confirmed via the claude-api reference, 2026-07-21). Manually updated, like RISK_FREE_RATE.
PRICING = {
    "claude-haiku-4-5": {"input": 1.00 / 1_000_000, "output": 5.00 / 1_000_000},
}
USD_TO_INR = 86.0  # approximate; manually updated for the ₹-per-query figure


class Usage:
    """Accumulates per-model token usage across the several model calls a single query makes
    (router + agent turns + judges). Pass one instance through a query, then .cost()/.summary()."""

    def __init__(self):
        self.calls = []  # list of (model, input_tokens, output_tokens)

    def add(self, model: str, resp) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        self.calls.append((model, int(getattr(u, "input_tokens", 0) or 0),
                           int(getattr(u, "output_tokens", 0) or 0)))

    def totals(self) -> tuple[int, int]:
        return (sum(c[1] for c in self.calls), sum(c[2] for c in self.calls))

    def models(self) -> list[str]:
        seen = []
        for m, _, _ in self.calls:
            if m not in seen:
                seen.append(m)
        return seen

    def cost_usd(self) -> float:
        total = 0.0
        for model, inp, out in self.calls:
            p = PRICING.get(model)
            if p:
                total += inp * p["input"] + out * p["output"]
        return round(total, 6)


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
