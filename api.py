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
    try:
        # answer_query routes first: advice/offtopic short-circuit before any tool runs.
        result = agent.answer_query(client(), list(history), message, {})
    except Exception:
        # Missing/invalid API key or upstream outage must degrade, not 500.
        return JSONResponse(status_code=503, content={
            "answer": "The analyst brain is unreachable right now (configuration or "
                      "upstream issue). The deterministic endpoints (/overview, /digest) "
                      "still work — or try again in a minute."})
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


def _nifty_window_return(first_flow: date | None) -> dict | None:
    """NIFTY 50 point-to-point return over the SAME window as the portfolio's life, so the
    dashboard can show a market reference next to XIRR. Labelled honestly in the UI: this is
    an index point-to-point figure, not a money-weighted one."""
    if not first_flow:
        return None
    try:
        pmap = agent._price_map("yfinance", agent.NIFTY50_SYMBOL, first_flow, date.today())
        if len(pmap) < 2:
            return None
        d0, d1 = min(pmap), max(pmap)
        start, end = pmap[d0], pmap[d1]
        years = max((d1 - d0).days / 365.0, 0.08)
        total = (end / start - 1) * 100
        annualised = ((end / start) ** (1 / years) - 1) * 100 if years >= 1 else total
        return {"symbol": "NIFTY 50", "from": d0.isoformat(), "to": d1.isoformat(),
                "total_pct": round(total, 2), "annualised_pct": round(annualised, 2),
                "annualised": years >= 1}
    except Exception:
        return None


@app.post("/analysis")
@app.get("/analysis")
async def analysis(request: Request, file: UploadFile | None = None):
    """Everything the dashboard needs, in one deterministic call: snapshot + month-end value
    timeline + the report engine's computed sections (allocation, diversification,
    concentration, overlap, cost, questions) + a market reference. No LLM, so it's free to
    serve — the AI only ever appears in /chat and the Excel report's insight blocks."""
    try:
        path, is_temp = await _portfolio_path_from(request, file)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    try:
        from report import data as report_data

        holdings, skipped = agent.read_portfolio(path)
        captures: list[dict] = []
        rows = []
        for h in holdings:
            cap: dict = {}
            rows.append(agent.reconstruct_holding(h, capture=cap))
            captures.append(cap)
        snap = agent.aggregate_snapshot(rows)
        snap["portfolio_xirr_pct"] = agent._portfolio_xirr(holdings, rows)

        level = report_data.detect_level(holdings)
        cache = report_data._load_cache()
        wrows = report_data.holdings_with_weights(holdings, level)
        total_value = sum(r["value"] for r in wrows)

        def safe(fn, *a):
            try:
                return fn(*a)
            except Exception as e:
                return {"error": f"data unavailable: {e}"}

        sections = {"overlap": safe(report_data.section_overlap, wrows, cache)}
        penalty = min((sections["overlap"].get("max_overlap_pct") or 0) / 100, 1)
        sections["allocation"] = safe(report_data.section_allocation, wrows, cache)
        sections["diversification"] = safe(report_data.section_diversification, wrows, penalty)
        sections["concentration"] = safe(report_data.section_concentration, wrows, cache)
        if report_data.LEVELS[level] >= 2:
            sections["cost"] = safe(report_data.section_cost, wrows, cache, total_value)
        sections["questions"] = safe(report_data.section_questions, sections)
        report_data._save_cache(cache)

        first = min((date(y, m, 1) for h in holdings for (y, m, _) in h["inflows"]),
                    default=None)
        return {
            "level": level,
            "snapshot": snap,
            "timeline": agent.portfolio_timeline(captures) if captures else [],
            "sections": sections,
            "market": _nifty_window_return(first),
            "holdings_priced": snap["priced_count"],
            "holdings_total": snap["total_count"],
            "skipped_rows": skipped,
        }
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"analysis failed: {e}"})
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
