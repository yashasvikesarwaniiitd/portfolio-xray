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
import os
import sqlite3
import tempfile
from datetime import date

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import agent
import logger
import metrics
import storage

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
            # An IP address is personal data and the counter only ever needs TODAY's row,
            # so yesterday's are deleted rather than accumulated indefinitely.
            conn.execute("DELETE FROM rate_limit WHERE day < ?", (today,))
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

def _quiet_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _csv_to_temp(csv_text: str) -> str:
    """Park an uploaded portfolio in a temp file for this request, and validate it the way
    the engine will actually read it.

    This used to run its own pd.read_csv and cap on len(df), which was wrong twice over
    once broker statements became a supported input. A statement carries banner lines
    above the header, so it is not rectangular and pandas raised a ParserError that was
    not a ValueError and so escaped the friendly-error path. And len(df) counts
    TRANSACTIONS, so a hundred trades in a single stock was rejected as "100 holdings".

    Writing the file first and asking read_portfolio means one parser decides what the
    file is, for every supported format. `resolve_funds=False` keeps this validation
    network-free — AMFI lookups happen once, in the endpoint that actually values things.
    """
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                    encoding="utf-8")
    f.write(csv_text)
    f.close()
    try:
        holdings, _ = agent.read_portfolio(f.name, resolve_funds=False)
    except ValueError as e:
        os.unlink(f.name)
        raise ValueError(str(e))
    except Exception as e:                     # never a 500 because of a user's file
        os.unlink(f.name)
        raise ValueError(f"Couldn't read that file: {e}")
    if len(holdings) > HOLDINGS_CAP:
        os.unlink(f.name)
        raise ValueError(f"That's {len(holdings)} holdings — this demo caps portfolios "
                         f"at {HOLDINGS_CAP}.")
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
    # The chat used to ignore the user's portfolio entirely and answer about the server
    # default, so the dashboard and the conversation beside it described different books.
    # The CSV arrives in the JSON body (the frontend already holds it in session state),
    # is validated the same way an upload is, and is deleted in the finally below.
    csv_text = body.get("portfolio_csv")
    temp_path = None
    if csv_text:
        try:
            temp_path = _csv_to_temp(str(csv_text))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"answer": str(e)})
    try:
        # answer_query routes first: advice/offtopic short-circuit before any tool runs.
        result = agent.answer_query(client(), list(history), message, {},
                                    path=temp_path or agent.DEFAULT_PORTFOLIO)
    except Exception:
        # Missing/invalid API key or upstream outage must degrade, not 500.
        return JSONResponse(status_code=503, content={
            "answer": "The analyst brain is unreachable right now (configuration or "
                      "upstream issue). The deterministic endpoints (/overview, /digest) "
                      "still work — or try again in a minute."})
    finally:
        if temp_path:
            _quiet_unlink(temp_path)
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
            _quiet_unlink(path)


def _nifty_window_return(first_flow: date | None, holdings: list | None = None,
                         rows: list | None = None) -> dict | None:
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
        out = {"symbol": "NIFTY 50", "from": d0.isoformat(), "to": d1.isoformat(),
               "total_pct": round(total, 2), "annualised_pct": round(annualised, 2),
               "annualised": years >= 1}
        # The like-for-like figure: the SAME money on the SAME dates, put into the index
        # instead. The point-to-point numbers above are kept, but they answer a different
        # question (how did the index move since your first purchase) and are not comparable
        # with a money-weighted return when most of the money went in recently.
        if holdings is not None and rows is not None:
            trade, terminal = agent._portfolio_flows(holdings, rows)
            end = max((d for d, _ in terminal), default=d1)
            same = agent.index_equivalent_xirr(trade, end, pmap)
            if same is not None:
                out["same_money_xirr_pct"] = same
        return out
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
        # Must precede section_questions, which reads it to raise a look-through question.
        sections["lookthrough"] = safe(report_data.section_lookthrough, wrows, cache)
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
            "market": _nifty_window_return(first, holdings, rows),
            "holdings_priced": snap["priced_count"],
            # Open positions only: a holding you have fully sold is not unpriced, and counting
            # it made "34 of 35 priced" read as a failure to value something.
            "holdings_total": snap["total_count"] - snap["closed_count"],
            "holdings_closed": snap["closed_count"],
            "skipped_rows": skipped,
        }
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"analysis failed: {e}"})
    finally:
        if is_temp:
            _quiet_unlink(path)


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
    # The generated workbook holds the user's ENTIRE portfolio - holdings, values, P&L,
    # charts, AI insights. It cannot be unlinked before FileResponse streams it, and the
    # finally below only ever removed the uploaded CSV, so every report left a complete
    # portfolio on the server's disk for good. That directly contradicts the "nothing
    # stored" promise. A BackgroundTask deletes it once the response has been sent.
    delivered = False
    try:
        from report import generate_report
        res = generate_report(path, out.name, use_ai=not bool(no_ai))
        delivered = True
        return FileResponse(out.name, filename="Portfolio_Health_Report.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument."
                                       "spreadsheetml.sheet",
                            headers={"X-Report-Level": res["level"],
                                     "X-Report-Cost-INR":
                                         str(res["insight_cost"]["est_cost_inr"])},
                            background=BackgroundTask(_quiet_unlink, out.name))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    finally:
        if is_temp:
            _quiet_unlink(path)
        if not delivered:
            # Generation failed, so no response will carry the background task away.
            _quiet_unlink(out.name)


# --- saved portfolios ----------------------------------------------------------------------
# The only user data this service keeps. Ownership is a secret the BROWSER holds: the
# client sends it in X-Owner-Key, the server stores its SHA-256, and a row is reachable by
# whoever presents the key and nobody else. See storage.py for why there are no accounts.
#
# The key travels in a header rather than the path or a query string because URLs are
# logged by proxies and land in browser history; headers are not written to Render's
# request log.

def _owner(request: Request) -> str:
    return (request.headers.get("X-Owner-Key") or "").strip()


@app.get("/portfolios")
def portfolios_list(request: Request):
    """Metadata only, never the CSV: a listing must not double as a bulk export."""
    if not storage.configured():
        return {"enabled": False, "portfolios": []}
    return {"enabled": True, "portfolios": storage.list_for(_owner(request))}


@app.get("/storage/status")
def storage_status():
    """Whether saving can actually work, and a coarse reason if not. `enabled` on /portfolios
    only says the connection string is SET; this says whether it WORKS. Labels only - see
    storage.diagnose for why no error text is returned."""
    return storage.diagnose()


@app.post("/portfolios")
async def portfolios_save(request: Request):
    if not storage.configured():
        return JSONResponse(status_code=503, content={"error": "Saving is not switched on."})
    key = _owner(request)
    if not key:
        return JSONResponse(status_code=400, content={"error": "No owner key."})
    body = await request.json()
    csv_text = str(body.get("csv_text") or "")
    if not csv_text.strip():
        return JSONResponse(status_code=400, content={"error": "Nothing to save."})
    # Validate before storing. A file the engine cannot read is not worth keeping, and
    # finding that out on LOAD would make a saved portfolio a trap rather than a shortcut.
    # _csv_to_temp already runs the real parser; a second read here re-resolved every fund
    # name against AMFI over the network, which an auto-save does on EVERY upload.
    try:
        _quiet_unlink(_csv_to_temp(csv_text))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    pid = storage.save(key, str(body.get("name") or "Portfolio"), csv_text)
    if not pid:
        return JSONResponse(status_code=400, content={
            "error": f"Could not save — at most {storage.MAX_PER_OWNER} portfolios per "
                     "browser, and the file must be under "
                     f"{storage.MAX_CSV_BYTES // 1000}KB. Delete one and try again."})
    return {"id": pid}


@app.get("/portfolios/{pid}")
def portfolios_load(pid: str, request: Request):
    if not storage.configured():
        return JSONResponse(status_code=503, content={"error": "Saving is not switched on."})
    got = storage.load(_owner(request), pid)
    if not got:
        # Deliberately the same answer for "no such row" and "not yours": distinguishing
        # them would confirm that an id exists to someone holding the wrong key.
        return JSONResponse(status_code=404, content={"error": "Not found."})
    return got


@app.put("/portfolios/{pid}")
async def portfolios_update(pid: str, request: Request):
    """Replace a saved portfolio's contents and/or rename it, in place."""
    if not storage.configured():
        return JSONResponse(status_code=503, content={"error": "Saving is not switched on."})
    body = await request.json()
    name = body.get("name")
    csv_text = body.get("csv_text")
    if csv_text is not None:
        try:
            _quiet_unlink(_csv_to_temp(str(csv_text)))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
    ok = storage.update(_owner(request), pid,
                        name=None if name is None else str(name),
                        csv_text=None if csv_text is None else str(csv_text))
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Not found."})
    return {"updated": True}


@app.post("/convert")
async def convert_workbook(file: UploadFile):
    """Turn an uploaded Excel workbook into portfolio text, ready for /analysis or /merge.

    Everything downstream speaks CSV text, so Excel support is a translation at the door
    rather than a second path through analysis, merging and saving. Stateless, like /merge:
    nothing is stored and no market data is fetched.

    Reads at most the limit plus one byte. Streamlit allows 200 MB uploads and this runs on
    a free-tier instance, so reading the whole body first would let one upload take the
    server down; the extra byte is what tells "exactly at the limit" from "over it".
    """
    import excel_import
    data = await file.read(excel_import.MAX_BYTES + 1)
    try:
        text, report = excel_import.workbook_to_text(data)
        _quiet_unlink(_csv_to_temp(text))       # the engine must be able to read the result
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"csv_text": text, "report": report, "summary": excel_import.describe(report)}


@app.post("/merge")
async def merge_portfolio(request: Request):
    """Fold an uploaded file into an existing portfolio's text, without double-counting.

    Stateless on purpose: the browser already holds the portfolio's text, so the server
    needs neither storage nor an owner key to do this, and it works with saving switched
    off. It returns the merged text and a report of what actually changed, and does NOT
    analyse - the caller sends the result to /analysis, which keeps this call free of any
    market-data network traffic.
    """
    body = await request.json()
    existing = str(body.get("existing_csv") or "")
    incoming = str(body.get("incoming_csv") or "")
    if not incoming.strip():
        return JSONResponse(status_code=400, content={"error": "The new file is empty."})
    try:
        import portfolio_merge
        merged, report = portfolio_merge.merge(existing, incoming)
        # Validate the RESULT the way the engine will read it, so a merge that produces
        # something unreadable fails here with a reason rather than at analysis time.
        _quiet_unlink(_csv_to_temp(merged))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if len(merged.encode("utf-8")) > storage.MAX_CSV_BYTES:
        return JSONResponse(status_code=400, content={
            "error": "This portfolio would be larger than the "
                     f"{storage.MAX_CSV_BYTES // 1000}KB limit once merged."})
    return {"csv_text": merged, "report": report,
            "summary": portfolio_merge.describe(report)}


@app.delete("/portfolios/{pid}")
def portfolios_delete(pid: str, request: Request):
    if not storage.configured():
        return JSONResponse(status_code=503, content={"error": "Saving is not switched on."})
    return {"deleted": storage.delete(_owner(request), pid)}
