"""ToolPort over the existing agent.py / report/ code, with the portfolio path threaded
through explicitly and priced once per run.

Why the cache exists. Every portfolio tool in agent.py independently calls read_portfolio()
and then reconstruct_holding() for every holding — so `portfolio_snapshot`, `portfolio_xirr`
and `concentration` each refetch the entire price history from yfinance and AMFI. One
investigation that calls five portfolio tools would do five full sweeps. On the sample
portfolio that is roughly 30 network round trips where 6 would do, and it would eat the
120-second wall-clock budget before the agent had finished thinking.

PortfolioContext does one read and one reconstruction pass, keeps each holding's captured
{price_map, priced_flows}, and serves every portfolio tool from it. That also makes
attribute_move free: the decomposition needs exactly the data the reconstruction already
fetched. Legacy code is untouched — this is a wrapper, not a rewrite.
"""
from __future__ import annotations

import json
import socket
import time
from datetime import date, datetime, timedelta

import agent
import metrics
from ..domain.attribution import attribute_move
from ..ports.tools import ToolResult

# Substrings that mark a failure as worth retrying. A symbol that does not exist will never
# exist; a socket that timed out might work in two seconds.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "connection", "temporarily", "rate limit", "too many requests",
    "502", "503", "504", "max retries", "ssl", "remote end closed", "unreachable",
)
_TRANSIENT_TYPES = (socket.timeout, ConnectionError, TimeoutError)


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


class PortfolioContext:
    """One portfolio, read once and priced once, for the life of a single investigation."""

    def __init__(self, path: str):
        self.path = path
        self._loaded = False
        self.holdings: list[dict] = []
        self.rows: list[dict] = []
        self.captures: list[dict] = []
        self.skipped: list[str] = []
        self.fetch_seconds = 0.0

    def load(self) -> None:
        if self._loaded:
            return
        started = time.perf_counter()
        self.holdings, self.skipped = agent.read_portfolio(self.path)
        self.rows, self.captures = [], []
        for h in self.holdings:
            cap: dict = {}
            row = agent.reconstruct_holding(h, capture=cap)
            self.rows.append(row)
            self.captures.append({
                "name": h["name"],
                "price_map": cap.get("price_map") or {},
                "priced_flows": cap.get("priced_flows") or [],
                "invested": h["total_invested"],
            })
        self.fetch_seconds = round(time.perf_counter() - started, 2)
        self._loaded = True

    def snapshot(self) -> dict:
        self.load()
        snap = agent.aggregate_snapshot(self.rows)
        snap["portfolio_xirr_pct"] = agent._portfolio_xirr(self.holdings, self.rows)
        if self.skipped:
            snap["skipped_rows"] = self.skipped
        return snap

    def find(self, symbol_or_name: str) -> tuple[dict, dict] | tuple[None, None]:
        self.load()
        q = (symbol_or_name or "").strip().lower()
        for h, cap in zip(self.holdings, self.captures):
            if q and (q == (h["symbol"] or "").lower() or q in h["name"].lower()):
                return h, cap
        return None, None


def _nifty_price_map(start: date, end: date) -> dict:
    """Index levels over the window, for the like-for-like comparison in attribute_move."""
    try:
        return agent._price_map("yfinance", agent.NIFTY50_SYMBOL,
                                start - timedelta(days=10), end)
    except Exception:
        return {}


def _parse_day(value, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{label} must be a date as YYYY-MM-DD, got {value!r}")


# --- tool schemas --------------------------------------------------------------------------
# The repo's only tool table. It was a deliberate subset of agent.TOOLS while the chat had
# its own; that table is deleted and this one serves both paths.
#
# Two tools from it were left out and stay out: the report builder and the weekly digest
# are long fixed pipelines of their own, and handing an agent on a step budget a tool that
# takes a minute and writes a file is a good way to waste the whole budget on one call.
# The /report and /digest endpoints call them directly, which is the right shape for them.
#
# The four single-value lookups (get_price, get_index, get_return, get_nav) were missing for
# a different reason: nobody ported them, because an investigation into why a portfolio
# moved never needs them. The chat path does - "what is the NAV of Parag Parikh?" is the
# README's own example - and it could not be answered here at all until this loop started
# serving chat too. Their exclusion was an accident; the two above are a decision.

SCHEMAS = [
    {
        "name": "attribute_move",
        "description":
            "THE PRIMARY TOOL for explaining a change in portfolio value. Decomposes the "
            "move over a window into: market_effect (gain on units already held), new_money "
            "(SIPs that arrived during the window), and gain_on_new_money. These sum to the "
            "total change exactly. Also returns per-holding contributions ranked by impact, "
            "the NIFTY 50 return over the same dates, the share of the portfolio it could "
            "price (coverage_pct), and any holdings it could not. Use this FIRST when asked "
            "why a portfolio is up, down, or lagging. Dates are YYYY-MM-DD.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "window start, YYYY-MM-DD"},
                "end": {"type": "string", "description": "window end, YYYY-MM-DD"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "portfolio_snapshot",
        "description":
            "Value the whole portfolio now by exact unit reconstruction (units per SIP = "
            "amount / price on that SIP's date), with per-holding invested, current value, "
            "P&L and XIRR, plus portfolio totals. Holdings with no live price are listed as "
            "unavailable and excluded from totals, never guessed.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "portfolio_xirr",
        "description": "Money-weighted annualised return (XIRR) for the portfolio and for "
                       "each holding. Accounts for when each SIP went in.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "holding_units",
        "description": "Exact units, per-SIP breakdown (date, amount, price, units), current "
                       "value and XIRR for ONE holding. Use when a single holding needs "
                       "explaining. Accepts a ticker/AMFI code or part of the holding name.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string",
                                      "description": "ticker, AMFI code, or holding name"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "concentration",
        "description": "How concentrated the portfolio is: per-holding weights, HHI, "
                       "effective number of holdings, and any position above 10%.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fund_overlap",
        "description":
            "Pairwise overlap between the funds/ETFs held. Every pair carries a 'basis' "
            "and the two bases are NOT comparable, so always report which one a number "
            "came from. basis=full_holdings_isin is the share of each fund's VALUE that "
            "is the same securities as the other, joined on ISIN from the AMC's full "
            "monthly SEBI disclosure — it also carries count_overlap_pct (shared names, "
            "which runs much higher) and shared_top. basis=top10_names is shared names in "
            "a disclosed top-10 only, and understates true overlap. The disclosure is a "
            "month-end snapshot: quote its 'as_of' date. Funds listed under "
            "funds_without_holdings or opaque_funds have UNKNOWN overlap, never zero.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fund_lookthrough",
        "description":
            "The companies the portfolio actually owns, counted through the funds that "
            "hold them: for each company, its weight inside each fund times that fund's "
            "weight in the portfolio. Use this to answer 'how much of X do I really own' "
            "and to find a single company reached through several funds. Only funds with "
            "a full disclosed holdings list are included — read portfolio_pct_covered and "
            "say what share of the book this could see, and quote the as_of date.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "beta",
        "description": "A stock's or ETF's beta against the NIFTY 50 over a period, by OLS "
                       "regression on daily returns. Not applicable to mutual funds.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "yfinance ticker"},
                           "period": {"type": "string",
                                      "description": "e.g. 1y, 2y (default 2y)"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "sharpe",
        "description": "Portfolio risk-adjusted return: annualised return and volatility over "
                       "the last year, and the Sharpe ratio against the RBI 10Y G-Sec yield.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "compare_returns",
        "description": "Point-to-point return of two tickers over the same period, for a "
                       "like-for-like comparison (e.g. a holding against ^NSEI).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker_a": {"type": "string"}, "ticker_b": {"type": "string"},
                           "period": {"type": "string", "description": "e.g. 6mo, 1y"}},
            "required": ["ticker_a", "ticker_b", "period"],
        },
    },
    {
        "name": "price_history",
        "description": "Downsampled closing-price series for a ticker over a period — use to "
                       "see the SHAPE of a move (steady drift vs one bad day).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"},
                           "period": {"type": "string", "description": "e.g. 1mo, 6mo, 1y"}},
            "required": ["ticker", "period"],
        },
    },
    {
        "name": "get_news",
        "description":
            "Recent cited headlines (headline, source, date, URL) from Google News. Search by "
            "COMPANY NAME, not ticker. News is reported ALONGSIDE a move as context the "
            "reader can check — never asserted as its cause.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "company or topic name"}},
            "required": ["query"],
        },
    },
    {
        "name": "load_portfolio",
        "description": "List the holdings in the portfolio with source, type, market and "
                       "total invested. Cheap, no prices. Use to learn what is held.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_fund",
        "description": "Resolve a mutual fund name to its AMFI scheme code. Direct Growth "
                       "variants are listed first. Never ask the user for a scheme code.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "part of the fund name"}},
            "required": ["query"],
        },
    },
    # --- single-value lookups, carried over verbatim from the chat's old tool table --------
    {
        "name": "get_price",
        "description": "Get the latest closing price for a stock by its yfinance ticker "
                       "symbol, e.g. RELIANCE.NS or INFY.NS for NSE stocks, AAPL for US.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string",
                                      "description": "yfinance ticker symbol"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_index",
        "description": "Get the latest level of a market index. Currently only the "
                       "NIFTY 50 is supported.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string",
                                    "description": "Index name, e.g. 'NIFTY 50'"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_return",
        "description": "Get the percentage return of ONE stock or index over a period, "
                       "computed from closing prices. Use ticker '^NSEI' for the NIFTY 50 "
                       "index. To compare two instruments, use compare_returns instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "yfinance ticker symbol"},
                "period": {"type": "string",
                           "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y"],
                           "description": "Lookback period"},
            },
            "required": ["ticker", "period"],
        },
    },
    {
        "name": "get_nav",
        "description": "Get the latest NAV (net asset value) of an Indian mutual fund by "
                       "its AMFI scheme code (e.g. 120503), sourced from the AMFI daily "
                       "NAV feed.",
        "input_schema": {
            "type": "object",
            "properties": {"fund_code": {"type": "string",
                                         "description": "AMFI mutual fund scheme code, "
                                                        "e.g. '120503'"}},
            "required": ["fund_code"],
        },
    },
]


class LegacyTools:
    """ToolPort over agent.py + report/, scoped to one portfolio for one run."""

    def __init__(self, portfolio_path: str, today: date | None = None):
        self.ctx = PortfolioContext(portfolio_path)
        self.today = today or date.today()
        self._meta_cache: dict = {}   # yfinance metadata, shared by fund_overlap

    def schemas(self) -> list[dict]:
        return SCHEMAS

    def execute(self, name: str, args: dict) -> ToolResult:
        try:
            return ToolResult(content=self._dispatch(name, args))
        except ValueError as e:
            # Our own validation and agent.read_portfolio's errors: actionable, not transient.
            return ToolResult(content=f"Error: {e}", is_error=True, transient=False)
        except Exception as e:
            return ToolResult(content=f"Error: {e}", is_error=True, transient=is_transient(e))

    # --- dispatch -------------------------------------------------------------------------
    def _dispatch(self, name: str, args: dict) -> str:
        if name == "attribute_move":
            return self._attribute_move(args)
        if name == "portfolio_snapshot":
            return json.dumps(self.ctx.snapshot())
        if name == "portfolio_xirr":
            return self._xirr()
        if name == "holding_units":
            return self._holding_units(args["symbol"])
        if name == "concentration":
            return self._concentration()
        if name == "fund_overlap":
            return self._fund_overlap()
        if name == "fund_lookthrough":
            return self._fund_lookthrough()
        if name == "load_portfolio":
            return agent.load_portfolio(self.ctx.path)
        if name == "beta":
            return agent.fetch_beta(args["symbol"], args.get("period", "2y"))
        if name == "sharpe":
            return agent.fetch_sharpe(self.ctx.path)
        if name == "compare_returns":
            return agent.fetch_compare(args["ticker_a"], args["ticker_b"], args["period"])
        if name == "price_history":
            return agent.fetch_price_history(args["ticker"], args["period"])
        if name == "get_news":
            return agent.fetch_news_tool(args["query"])
        if name == "search_fund":
            return agent.search_fund(args["query"])
        if name == "get_price":
            return agent.fetch_close(args["ticker"])
        if name == "get_index":
            # The schema takes a name for the model's benefit; only one index is wired, and
            # the chat's old dispatcher ignored the argument in exactly the same way.
            return agent.fetch_close(agent.NIFTY50_SYMBOL)
        if name == "get_return":
            return json.dumps(agent.compute_return(args["ticker"], args["period"]))
        if name == "get_nav":
            return agent.fetch_nav(args["fund_code"])
        raise ValueError(f"unknown tool: {name}")

    # --- implementations ------------------------------------------------------------------
    def _attribute_move(self, args: dict) -> str:
        start = _parse_day(args.get("start"), "start")
        end = _parse_day(args.get("end"), "end")
        if end > self.today:
            end = self.today
        if start >= end:
            raise ValueError(f"start ({start}) must be before end ({end})")
        self.ctx.load()
        out = attribute_move(self.ctx.captures, start, end,
                             benchmark_price_map=_nifty_price_map(start, end))
        return json.dumps(out)

    def _xirr(self) -> str:
        self.ctx.load()
        per_holding = [{"name": r["name"], "xirr_pct": r.get("xirr_pct")}
                       for r in self.ctx.rows if r.get("status") == "priced"]
        return json.dumps({
            "portfolio_xirr_pct": agent._portfolio_xirr(self.ctx.holdings, self.ctx.rows),
            "holdings": per_holding,
            "method": "Money-weighted (XIRR) over every priced SIP cashflow, solved in Python.",
        })

    def _holding_units(self, symbol: str) -> str:
        h, cap = self.ctx.find(symbol)
        if h is None:
            names = ", ".join(x["name"] for x in self.ctx.holdings)
            raise ValueError(f"no holding matches {symbol!r}. Holdings are: {names}")
        idx = self.ctx.holdings.index(h)
        row = dict(self.ctx.rows[idx])
        # The shared pass skips the per-SIP breakdown to keep snapshots small; rebuild it
        # from the captured flows rather than refetching every price.
        flows = cap.get("priced_flows") or []
        if flows:
            row["breakdown"] = metrics.reconstruct_units(flows)["breakdown"]
        return json.dumps(row)

    def _concentration(self) -> str:
        """Same output as agent.fetch_concentration, served from the cached pass."""
        self.ctx.load()
        priced = [{"name": r["name"], "value": r["current_value"], "market": r["market"],
                   "type": r["type"], "risk": r["risk"]}
                  for r in self.ctx.rows if r.get("status") == "priced"]
        if not priced:
            raise ValueError("No priced holdings available to measure concentration.")
        stats = metrics.concentration_stats(priced)
        stats["priced_count"] = len(priced)
        stats["total_count"] = len(self.ctx.rows)
        stats["unavailable"] = [r["name"] for r in self.ctx.rows
                                if r.get("status") != "priced"]
        stats["note"] = ("Weights are by current value. HHI is the sum of squared fractional "
                         "weights (higher = more concentrated); effective_holdings = 1/HHI.")
        return json.dumps(stats)

    def _fund_overlap(self) -> str:
        from report import data as report_data
        self.ctx.load()
        out = report_data.section_overlap(self.ctx.holdings, self._meta_cache)
        return json.dumps(out)

    def _fund_lookthrough(self) -> str:
        from report import data as report_data
        self.ctx.load()
        out = report_data.section_lookthrough(self.ctx.holdings, self._meta_cache)
        return json.dumps(out)
