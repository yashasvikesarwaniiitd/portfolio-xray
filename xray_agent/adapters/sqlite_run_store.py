"""Persist investigations to the existing SQLite file, so a run can be inspected after the
fact without Langfuse and without any network.

Two tables beside the existing query_log: investigation_runs (one row per run, with the
scores an eval cares about) and investigation_steps (the ordered trace). Like logger.py, a
storage failure is swallowed — observability must never be the thing that breaks a run.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import logger

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS investigation_runs (
    run_id            TEXT PRIMARY KEY,
    ts                TEXT NOT NULL,
    question          TEXT,
    portfolio         TEXT,
    outcome           TEXT,
    steps_used        INTEGER,
    cost_inr          REAL,
    elapsed_s         REAL,
    findings_total    INTEGER,
    findings_verified INTEGER,
    verified_ratio    REAL,
    evidence_count    INTEGER,
    errors            INTEGER,
    summary           TEXT
)
"""

STEPS_DDL = """
CREATE TABLE IF NOT EXISTS investigation_steps (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    step_no  INTEGER,
    kind     TEXT,
    payload  TEXT
)
"""


class SqliteRunStore:
    def __init__(self, path: str | None = None):
        self.path = path or logger.DB_PATH

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.execute(RUNS_DDL)
        conn.execute(STEPS_DDL)
        return conn

    def save_step(self, run_id: str, step_no: int, kind: str, payload: dict) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO investigation_steps (run_id, step_no, kind, payload) "
                    "VALUES (?,?,?,?)",
                    (run_id, step_no, kind, json.dumps(payload, default=str)))
        except Exception:
            pass

    def save_run(self, state) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO investigation_runs (run_id, ts, question, "
                    "portfolio, outcome, steps_used, cost_inr, elapsed_s, findings_total, "
                    "findings_verified, verified_ratio, evidence_count, errors, summary) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (state.run_id, datetime.now().isoformat(timespec="seconds"),
                     state.goal.question, state.goal.portfolio_path,
                     state.outcome.value if state.outcome else None,
                     state.steps_used, round(state.cost_inr, 6), round(state.elapsed(), 2),
                     len(state.findings), len(state.verified_findings),
                     round(state.verified_ratio(), 4), len(state.ledger),
                     len(state.errors), state.summary))
        except Exception:
            pass


def recent_runs(limit: int = 10, path: str | None = None) -> list[dict]:
    """Read back the last few runs — used by the CLI's --history flag and by the docs."""
    try:
        with sqlite3.connect(path or logger.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(RUNS_DDL)
            rows = conn.execute(
                "SELECT * FROM investigation_runs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
