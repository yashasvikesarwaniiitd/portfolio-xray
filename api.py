"""FastAPI layer over the existing Portfolio X-Ray logic — a THIN wrapper, nothing
reimplemented. All Anthropic calls happen here; the frontend never sees the API key.

Public-hardening (non-negotiable):
- SQLite per-IP daily rate limits: 20 chat turns, 3 reports; 60-holding and 500-char caps.
  Exceeded -> friendly JSON message, HTTP 200-shaped UX (429 status, polite body).
- Kill switch: XRAY_DISABLED=1 -> every endpoint (except /health) returns a maintenance
  message. Flip from the Render dashboard if costs spike.
- Nothing a user enters is stored: uploaded portfolios live in a temp file for the length
  of one request and are deleted; only aggregate token/cost counters persist.

Run: uv run uvicorn api:app --host 0.0.0.0 --port 8000
"""
import io
import os
import sqlite3
import tempfile
from datetime import date

import anthropic
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import agent
import logger
import metrics

VERSION = "1.0.0"
CHAT_LIMIT_PER_DAY = 20
REPORTS_PER_DAY = 3
HOLDINGS_CAP = 60
MESSAGE_CAP = 500

load_dotenv()
app = FastAPI(title="Portfolio X-Ray API", version=VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])  # Streamlit Cloud origin isn't fixed; API is public+rate-limited anyway

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# --- rate limiting (SQLite counter by IP, per UTC day) --------------------------------

def _bump(ip: str, kind: str, cap: int) -> bool:
    """Increment (ip, today, kind) and return True while within cap. Fail-open on DB
    hiccups — a broken counter should never take the demo down."""
    try:
        with sqlite3.connect(logger.DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS rate_limit "
                         "(ip TEXT, day TEXT, kind TEXT, n INTEGER, "
                         "PRIMARY KEY (ip, day, kind))")
            today = date.today().isoformat()
            conn.execute(
                "INSERT INTO rate_limit VALUES (?,?,?,1) "
                "ON CONFLICT(ip, day, kind) DO UPDATE SET n = n + 1",
                (ip, today, kind))
            n = conn.execute("SELECT n FROM rate_limit WHERE ip=? AND day=? AND kind=?",
                             (ip, today, kind)).fetchone()[0]
        return n <= cap
    except Exception:
        return True


def _friendly_limit(what: str, cap: int) -> JSONResponse:
    return JSONResponse(status_code=429, content={
        "answer": f"You've reached today's free limit of {cap} {what}. The limits keep "
                  "this demo affordable — please come back tomorrow, or clone the repo "
                  "and run it with your own API key.",
        "limited": True})


# --- kill switch -----------------------------------------------------------------------

@app.middleware("http")
async def kill_switch(request: Request, call_next):
    if os.environ.get("XRAY_DISABLED") == "1" and request.url.path != "/health":
        return JSONResponse(status_code=503, content={
            "answer": "Portfolio X-Ray is briefly down for maintenance. Back soon — the "
                      "repo and eval results are on GitHub in the meantime.",
            "maintenance": True})
    return await call_next(request)


# --- portfolio input handling ------------------------------------------------------------

def _csv_to_temp(csv_text: str) -> str:
    """Validate an uploaded/pasted portfolio CSV (holdings cap) and park it in a temp file
    for this request only. Raises ValueError with a friendly message."""
    df = pd.read_csv(io.StringIO(csv_text))
    if len(df) > HOLDINGS_CAP:
        raise ValueError(f"That's {len(df)} rows — this demo caps portfolios at "
                         f"{HOLDINGS_CAP} holdings.")
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                    encoding="utf-8")
    f.write(csv_text)
    f.close()
    return f.name


async def _portfolio_path_from(request: Request, file: UploadFile | None) -> tuple[str, bool]:
    """(path, is_temp). Uploaded CSV wins; otherwise the server's default (sample)."""
    if file is not None:
        raw = (await file.read()).decode("utf-8", errors="replace")
        return _csv_to_temp(raw), True
    return agent.DEFAULT_PORTFOLIO, False


# --- endpoints -----------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "disabled" if os.environ.get("XRAY_DISABLED") == "1" else "ok",
            "version": VERSION}


@app.get("/stats")
def stats():
    """Aggregate-only economics from the SQLite log — powers the live line in the UI."""
    s = logger.summarize()
    try:
        with sqlite3.connect(logger.DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*), AVG(est_cost_usd) FROM query_log "
                               "WHERE category='report' AND est_cost_usd > 0").fetchone()
        s["reports_generated"] = row[0] or 0
        s["avg_report_cost_inr"] = round((row[1] or 0) * logger.USD_TO_INR, 2)
    except Exception:
        pass
    return s


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    message = str(body.get("message", "")).strip()
    history = body.get("history") or []
    if not message:
        return JSONResponse(status_code=400, content={"answer": "Say something first :)"})
    if len(message) > MESSAGE_CAP:
        return JSONResponse(status_code=400, content={
            "answer": f"Messages are capped at {MESSAGE_CAP} characters for this demo — "
                      "try a shorter question."})
    ip = request.client.host if request.client else "unknown"
    if not _bump(ip, "chat", CHAT_LIMIT_PER_DAY):
        return _friendly_limit("chat turns", CHAT_LIMIT_PER_DAY)
    # answer_query routes first: advice/offtopic short-circuit before any tool runs.
    result = agent.answer_query(client(), list(history), message, {})
    return {"answer": result["answer"], "category": result["category"],
            "refused": result["refused"], "tools_used": result["tools_used"]}


@app.get("/digest")
def digest():
    return agent.build_digest(agent.DEFAULT_PORTFOLIO)


@app.post("/overview")
@app.get("/overview")
async def overview(request: Request, file: UploadFile | None = None):
    """Dashboard payload in ONE reconstruction pass: snapshot + portfolio XIRR +
    concentration (weights/HHI/effective bets) — no LLM involved, so it's free to serve."""
    try:
        path, is_temp = await _portfolio_path_from(request, file)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    try:
        holdings, _ = agent.read_portfolio(path)
        rows = [agent.reconstruct_holding(h) for h in holdings]
        snap = agent.aggregate_snapshot(rows)
        snap["portfolio_xirr_pct"] = agent._portfolio_xirr(holdings, rows)
        priced = [{"name": r["name"], "value": r["current_value"], "market": r["market"],
                   "type": r["type"], "risk": r["risk"]}
                  for r in rows if r.get("status") == "priced"]
        conc = metrics.concentration_stats(priced) if priced else {}
        return {"snapshot": snap, "concentration": {
            "hhi": conc.get("hhi"), "effective_holdings": conc.get("effective_holdings"),
            "top_weights": conc.get("holdings_by_weight", [])[:8],
            "over_threshold": conc.get("over_threshold", [])}}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    finally:
        if is_temp:
            try:
                os.unlink(path)
            except OSError:
                pass


@app.post("/report")
async def report(request: Request, file: UploadFile | None = None, no_ai: int = 0):
    ip = request.client.host if request.client else "unknown"
    if not _bump(ip, "report", REPORTS_PER_DAY):
        return _friendly_limit("health reports", REPORTS_PER_DAY)
    try:
        path, is_temp = await _portfolio_path_from(request, file)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    out = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    out.close()
    try:
        from report import generate_report
        res = generate_report(path, out.name, use_ai=not bool(no_ai))
        return FileResponse(out.name, filename="Portfolio_Health_Report.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument."
                                       "spreadsheetml.sheet",
                            headers={"X-Report-Level": res["level"],
                                     "X-Report-Cost-INR":
                                         str(res["insight_cost"]["est_cost_inr"])})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    finally:
        if is_temp:
            try:
                os.unlink(path)
            except OSError:
                pass
