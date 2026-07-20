"""Portfolio X-Ray agent loop: Anthropic Messages API + yfinance/mftool tools."""
import json
import os
import sys
from datetime import date, datetime, timedelta

import anthropic
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from mftool import Mftool

import metrics
import refusals
import router

MODEL = "claude-haiku-4-5"
NIFTY50_SYMBOL = "^NSEI"  # yfinance symbol for the NIFTY 50 index
# Risk-free rate for Sharpe = RBI 10Y G-Sec yield. No reliable free live API exists, so
# this is UPDATED MANUALLY on a monthly cadence (see CLAUDE.md). Last set 2026-07-21.
RISK_FREE_RATE = 0.068  # 6.8% p.a.
DEFAULT_PORTFOLIO = "portfolio.csv"
# Columns the portfolio CSV must have; the rest are monthly SIP-inflow columns
# (labelled like "Mar'25") detected by the apostrophe in their name.
REQUIRED_COLUMNS = ["Where", "symbol", "source", "Total Invested"]
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_MF = Mftool()  # AMFI daily NAV feed client; construction does no network I/O

SYSTEM = (
    "You are Portfolio X-Ray, a portfolio analytics assistant for Indian retail investors. "
    "Your job: explain what an investor's portfolio and the market are doing, in clear, "
    "plain-English, educational terms.\n\n"
    "Boundaries (absolute):\n"
    "- Analytics and education ONLY. Never give buy/sell/hold advice, recommendations, price "
    "targets, or predictions about what will happen. If asked whether to buy, sell, or hold "
    "something, decline and explain you provide analytics, not investment advice.\n"
    "- Never do financial arithmetic yourself. Python tools compute every number; you only "
    "decide which tools to call and explain the results. Report figures exactly as the tools "
    "return them. If no tool provides a number, say you can't compute it rather than "
    "calculating or estimating it in your head.\n\n"
    "Use the portfolio tools to answer questions about the user's own holdings, and the market "
    "tools for prices, returns, indices, history, and mutual-fund NAVs. When you show money, "
    "the figures are Indian Rupees (₹).\n\n"
    "Tools available:\n"
    "- portfolio_snapshot: whole-portfolio valuation and P&L (exact unit reconstruction) with "
    "per-holding and portfolio XIRR.\n"
    "- portfolio_xirr: money-weighted annualized return for the portfolio and each holding.\n"
    "- holding_units: exact units, per-SIP breakdown, value and XIRR for one holding.\n"
    "- beta: a stock/ETF's sensitivity to the NIFTY 50 (not applicable to mutual funds).\n"
    "- sharpe: portfolio risk-adjusted return.\n"
    "- concentration: how diversified the portfolio is (weights, HHI, >10% flags).\n"
    "- load_portfolio, get_price, get_index, get_return, compare_returns, get_nav, "
    "price_history for holdings listing and market data.\n\n"
    "Portfolio values now use EXACT unit reconstruction (units per SIP = amount ÷ price on that "
    "SIP's date, summed), valued at the latest available price — these are computed figures, not "
    "estimates. Values are 'as of' each holding's latest price date. Holdings marked "
    "'unavailable' (manual baskets, blank source, or newly-listed symbols with no data) have no "
    "live price — report them as such, never guess a value, and note they are excluded from "
    "totals. When you cite beta or Sharpe, briefly state what the number means; never turn it "
    "into a recommendation.\n\n"
    "A query router runs before you and only sends you analytics and education questions; "
    "advice-seeking and off-topic queries are handled elsewhere. Still, if any buy/sell/hold "
    "or prediction ask reaches you, decline it and offer analytics instead — you are the "
    "backstop, not the first line."
)

TOOLS = [
    {
        "name": "get_price",
        "description": "Get the latest closing price for a stock by its yfinance ticker "
                       "symbol, e.g. RELIANCE.NS or INFY.NS for NSE stocks, AAPL for US.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "yfinance ticker symbol"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_index",
        "description": "Get the latest level of a market index. Currently only the "
                       "NIFTY 50 is supported.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Index name, e.g. 'NIFTY 50'"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_return",
        "description": "Get the percentage return of ONE stock or index over a period, computed "
                       "from closing prices. Use ticker '^NSEI' for the NIFTY 50 index. To compare "
                       "two instruments, use compare_returns instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "yfinance ticker symbol"},
                "period": {"type": "string", "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y"],
                           "description": "Lookback period"},
            },
            "required": ["ticker", "period"],
        },
    },
    {
        "name": "compare_returns",
        "description": "Compare the percentage returns of two stocks/indices over the same period, "
                       "including the spread and which one outperformed. Always use this (never "
                       "subtract returns yourself) when comparing two instruments. Use '^NSEI' for "
                       "the NIFTY 50 index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker_a": {"type": "string", "description": "first yfinance ticker symbol"},
                "ticker_b": {"type": "string", "description": "second yfinance ticker symbol"},
                "period": {"type": "string", "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y"],
                           "description": "Lookback period"},
            },
            "required": ["ticker_a", "ticker_b", "period"],
        },
    },
    {
        "name": "load_portfolio",
        "description": "Load the user's portfolio from their CSV and list the holdings with their "
                       "name, symbol, data source (yfinance/mftool/crypto/manual), category, "
                       "amount invested, and number of SIP-inflow months. Use this to see what "
                       "the user owns before answering portfolio questions. Does NOT fetch prices "
                       "— use portfolio_snapshot for valuation.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "portfolio_snapshot",
        "description": "Value the user's whole portfolio. For each holding it routes to the right "
                       "provider by its 'source' (yfinance for stocks/ETFs, yfinance for crypto "
                       "pairs, mftool for mutual funds; 'manual'/blank are skipped). Because the "
                       "CSV records money invested (not units), current value is ESTIMATED: units "
                       "≈ invested ÷ average price over the accumulation window, valued at today's "
                       "price. Returns per-holding invested/estimated-value/P&L and portfolio "
                       "totals. Holdings with no live price appear as 'price unavailable' and are "
                       "excluded from totals — never dropped, never crashing the snapshot. All "
                       "math is in Python.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_nav",
        "description": "Get the latest NAV (net asset value) of an Indian mutual fund by its "
                       "AMFI scheme code (e.g. 120503), sourced from the AMFI daily NAV feed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fund_code": {"type": "string",
                              "description": "AMFI mutual fund scheme code, e.g. '120503'"},
            },
            "required": ["fund_code"],
        },
    },
    {
        "name": "price_history",
        "description": "Get the historical closing-price series for a stock or index over a "
                       "period, including start/end/high/low. Use '^NSEI' for the NIFTY 50. Use "
                       "this when the user wants to see how a price moved over time; use "
                       "get_return for just the percentage return.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "yfinance ticker symbol"},
                "period": {"type": "string",
                           "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "max"],
                           "description": "Lookback period"},
            },
            "required": ["ticker", "period"],
        },
    },
    {
        "name": "portfolio_xirr",
        "description": "Money-weighted annualized return (XIRR) of the portfolio and of each "
                       "priced holding, computed by exact unit reconstruction from the SIP "
                       "cashflows in the CSV. Use this for 'what return am I getting?' style "
                       "questions. All math is in Python.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "holding_units",
        "description": "For one holding (by its symbol/AMFI code), reconstruct the exact units "
                       "held from each SIP's price-on-date, with a per-SIP breakdown, current "
                       "value and XIRR. Use for 'how many units of X do I hold / how did this "
                       "one holding do?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string",
                           "description": "The holding's symbol or AMFI code as in the CSV, "
                                          "e.g. 'RELIANCE.NS' or '122639'"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "beta",
        "description": "Market beta of a stock/ETF: the regression slope of its daily returns "
                       "against the NIFTY 50 over ~2 years. yfinance tickers only — returns "
                       "'not applicable' for mutual-fund codes. Measures historical sensitivity "
                       "to the market, not a prediction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "yfinance ticker, e.g. RELIANCE.NS"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "sharpe",
        "description": "Portfolio Sharpe ratio: value-weighted annualized return minus the "
                       "risk-free rate (RBI 10Y G-Sec), over annualized volatility, from ~1y of "
                       "daily data. Reports its assumptions and how much of the portfolio by "
                       "value it could cover. All math is in Python.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "concentration",
        "description": "Portfolio concentration: weight of each holding by current value, HHI "
                       "(sum of squared weights) and effective number of holdings, weight by "
                       "Market/Type/Risk, and any single holding over 10%. Use for 'how "
                       "concentrated / diversified is my portfolio?'.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def fetch_close(symbol: str) -> str:
    hist = yf.Ticker(symbol).history(period="5d")
    if hist.empty:
        raise ValueError(f"No data for '{symbol}' — check the ticker symbol.")
    return json.dumps({
        "symbol": symbol,
        "close": round(float(hist["Close"].iloc[-1]), 2),
        "as_of": str(hist.index[-1].date()),
    })


def compute_return(symbol: str, period: str) -> dict:
    hist = yf.Ticker(symbol).history(period=period)
    if hist.empty:
        raise ValueError(f"No data for '{symbol}' — check the ticker symbol.")
    start, end = float(hist["Close"].iloc[0]), float(hist["Close"].iloc[-1])
    return {
        "symbol": symbol, "period": period,
        "start_close": round(start, 2), "end_close": round(end, 2),
        "return_pct": round((end - start) / start * 100, 2),
        "from": str(hist.index[0].date()), "to": str(hist.index[-1].date()),
    }


def fetch_compare(ticker_a: str, ticker_b: str, period: str) -> str:
    a, b = compute_return(ticker_a, period), compute_return(ticker_b, period)
    spread = round(a["return_pct"] - b["return_pct"], 2)
    return json.dumps({
        "a": a, "b": b, "spread_pct_a_minus_b": spread,
        "outperformer": a["symbol"] if spread > 0 else b["symbol"] if spread < 0 else "equal",
    })


def _to_number(value) -> float:
    """Parse a CSV cell into a number. Blank, '-', or NaN mean 'no amount' -> 0.0."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "nan", "NaN", "None"):
        return 0.0
    return float(s)


def parse_month_label(label: str) -> tuple[int, int]:
    """Parse a monthly-column label like "Mar'25" or "April'25" into (year, month).

    Month names are inconsistent (3-letter and full) so we key on the first 3 letters."""
    name, _, yy = label.strip().partition("'")
    key = name.strip()[:3].lower()
    if key not in _MONTHS or not yy.strip().isdigit():
        raise ValueError(f"unrecognised month label {label!r}")
    return 2000 + int(yy.strip()), _MONTHS[key]


def read_portfolio(path: str = DEFAULT_PORTFOLIO) -> tuple[list[dict], list[str]]:
    """Parse the portfolio CSV into holdings. Returns (holdings, skipped_row_notes).

    No network I/O, so it is unit-testable. Preserves each holding's source/symbol/category
    and its per-month SIP inflows. Raises ValueError (never crashes) on a missing file,
    missing required columns, or a file with no usable rows."""
    if not os.path.exists(path):
        raise ValueError(
            f"Portfolio file '{path}' not found. Expected columns include: "
            f"{', '.join(REQUIRED_COLUMNS)}, plus monthly inflow columns like \"Mar'25\"."
        )
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise ValueError(f"Could not read '{path}' as CSV: {e}")
    # Header may carry a BOM, embedded newlines, or doubled spaces — normalise whitespace.
    df.columns = [" ".join(str(c).replace("﻿", "").split()) for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Portfolio file is missing required column(s): {', '.join(missing)}."
        )
    month_cols = [c for c in df.columns if "'" in c]  # monthly SIP-inflow columns
    holdings, skipped = [], []
    for i, row in df.iterrows():
        line = int(i) + 2  # +1 for header, +1 for 1-based line numbers
        try:
            name = str(row["Where"]).strip()
            # Rows with no name are structural: blank spacers or the sheet's own summary
            # footer (e.g. "Overall Investments", "Hero Motocop"). Skip them silently —
            # they are not holdings and shouldn't clutter output or the skipped list.
            if not name or name.lower() == "nan":
                continue
            source = str(row["source"]).strip().lower()
            if source in ("nan", ""):
                source = ""  # blank source: no live-price provider
            symbol = row["symbol"]
            symbol = None if pd.isna(symbol) or str(symbol).strip().lower() in ("", "nan") \
                else str(symbol).strip()
            inflows = []
            for c in month_cols:
                amt = _to_number(row[c])
                if amt > 0:
                    y, m = parse_month_label(c)
                    inflows.append((y, m, amt))
            inflows.sort()
            holdings.append({
                "name": name,
                "symbol": symbol,
                "source": source,
                "type": str(row.get("Type", "")).strip(),
                "market": str(row.get("Market", "")).strip(),
                "risk": str(row.get("Risk", "")).strip(),
                "total_invested": _to_number(row["Total Invested"]),
                "inflows": inflows,
            })
        except (ValueError, TypeError, KeyError) as e:
            skipped.append(f"line {line}: {e}")
    if not holdings:
        detail = "; ".join(skipped) if skipped else "file has no data rows"
        raise ValueError(f"No valid holdings found ({detail}).")
    return holdings, skipped


def load_portfolio(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, skipped = read_portfolio(path)
    listed = [{
        "name": h["name"],
        "symbol": h["symbol"],
        "source": h["source"] or "(blank)",
        "type": h["type"],
        "market": h["market"],
        "total_invested": round(h["total_invested"], 2),
        "inflow_months": len(h["inflows"]),
    } for h in holdings]
    out = {"holdings": listed, "count": len(listed)}
    if skipped:
        out["skipped_rows"] = skipped
    return json.dumps(out)


# --- metrics engine: exact unit reconstruction, XIRR, beta, sharpe, concentration ---
# The heavy lifting (math) lives in metrics.py as pure functions; the functions below
# fetch data (mftool for funds, yfinance for stocks/ETFs/crypto) and orchestrate.

def _price_map(source: str, symbol: str, start: date, end: date) -> dict:
    """Fetch a {date: price} map of closes/NAVs in [start, end]. yfinance for
    stocks/ETFs/crypto pairs, mftool historical NAV for mutual funds. Empty dict if none."""
    if source in ("yfinance", "crypto"):
        hist = yf.Ticker(symbol).history(start=start.isoformat(),
                                         end=(end + timedelta(days=1)).isoformat())
        if hist.empty:
            return {}
        # yfinance often appends today's date with a NaN close before data publishes;
        # drop NaNs so max(pmap) is a real trading day, not a NaN "latest price".
        return {idx.date(): float(v) for idx, v in hist["Close"].items() if pd.notna(v)}
    if source == "mftool":
        raw = _MF.get_scheme_historical_nav(str(symbol))
        if not raw or not raw.get("data"):
            return {}
        out = {}
        for d in raw["data"]:
            try:
                dt = datetime.strptime(d["date"], "%d-%m-%Y").date()
                nav = float(d["nav"])
                if start <= dt <= end and nav == nav:  # nav == nav filters NaN
                    out[dt] = nav
            except (ValueError, KeyError, TypeError):
                continue
        return out
    return {}


def reconstruct_holding(h: dict, include_breakdown: bool = False) -> dict:
    """Value one holding by EXACT unit reconstruction, routing by source. SIPs are dated to
    the 1st of their month; the current value is dated to the latest available price/NAV
    date (via the walk-back). Catches its own errors and returns 'unavailable' rather than
    raising — one bad holding must never crash the snapshot. Set include_breakdown to attach
    the per-SIP price/units detail (used by the holding_units tool, omitted from snapshots)."""
    base = {
        "name": h["name"], "symbol": h["symbol"], "source": h["source"] or "(blank)",
        "type": h["type"], "market": h["market"], "risk": h["risk"],
    }
    if h["source"] in ("", "manual") or not h["symbol"]:
        return {**base, "status": "unavailable", "invested": round(h["total_invested"], 2),
                "reason": "no live-price source (manual basket or blank source)"}
    if not h["inflows"]:
        return {**base, "status": "unavailable", "invested": round(h["total_invested"], 2),
                "reason": "no recorded SIP inflows to reconstruct units from"}
    try:
        # Fetch a 15-day buffer before the first SIP so the weekend/holiday walk-back
        # (e.g. a SIP dated to a Saturday) has an earlier price to fall back to.
        start = date(h["inflows"][0][0], h["inflows"][0][1], 1) - timedelta(days=15)
        today = date.today()
        pmap = _price_map(h["source"], h["symbol"], start, today)
        if not pmap:
            return {**base, "status": "unavailable",
                    "invested": round(h["total_invested"], 2),
                    "reason": "no price data for this period (possibly newly listed)"}
        price_date = max(pmap)
        latest_price = pmap[price_date]
        priced_flows = [
            {"date": date(y, m, 1), "amount": amt,
             "price": metrics.nav_on_or_before(pmap, date(y, m, 1))}
            for (y, m, amt) in h["inflows"]
        ]
        rec = metrics.reconstruct_units(priced_flows)
        if rec["priced_sips"] == 0 or rec["total_units"] == 0:
            return {**base, "status": "unavailable",
                    "invested": round(h["total_invested"], 2),
                    "reason": "no SIP could be priced (instrument likely listed after the "
                              "SIP dates)"}
        invested = rec["invested_priced"]
        current_value = rec["total_units"] * latest_price
        pnl = current_value - invested
        # Per-holding XIRR: each priced SIP is a dated outflow, current value the final inflow.
        cashflows = [(f["date"], -f["amount"]) for f in priced_flows if f["price"]]
        cashflows.append((price_date, current_value))
        try:
            xirr_pct = round(metrics.xirr(cashflows) * 100, 2)
        except ValueError as e:
            xirr_pct = None
            xirr_note = str(e)
        row = {
            **base, "status": "priced",
            "units": round(rec["total_units"], 4),
            "invested": invested,
            "current_value": round(current_value, 2),
            "current_price": round(latest_price, 4),
            "price_date": str(price_date),
            "pnl_abs": round(pnl, 2),
            "pnl_pct": round(pnl / invested * 100, 2) if invested else 0.0,
            "xirr_pct": xirr_pct,
            "priced_sips": rec["priced_sips"], "total_sips": rec["total_sips"],
        }
        if rec["invested_unpriced"]:
            row["invested_unpriced"] = rec["invested_unpriced"]  # SIPs before price history
        if xirr_pct is None:
            row["xirr_note"] = xirr_note
        if include_breakdown:
            row["breakdown"] = rec["breakdown"]
        return row
    except Exception as e:  # any provider hiccup -> unavailable, never crash the snapshot
        return {**base, "status": "unavailable", "invested": round(h["total_invested"], 2),
                "reason": f"price fetch/reconstruction failed: {e}"}


def aggregate_snapshot(rows: list[dict]) -> dict:
    """Pure aggregation over reconstructed holdings, plus a whole-portfolio XIRR pooling
    every priced SIP cashflow. Totals include only 'priced' holdings; unavailable holdings'
    invested capital is reported separately."""
    priced = [r for r in rows if r.get("status") == "priced"]
    total_invested = sum(r["invested"] for r in priced)
    total_current = sum(r["current_value"] for r in priced)
    total_pnl = total_current - total_invested
    unpriced_invested = sum(r.get("invested", 0.0) for r in rows
                            if r.get("status") != "priced")
    return {
        "holdings": rows,
        "priced_count": len(priced),
        "total_count": len(rows),
        "total_invested": round(total_invested, 2),
        "total_current_value": round(total_current, 2),
        "total_pnl_abs": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested else 0.0,
        "unpriced_invested": round(unpriced_invested, 2),
        "method": "Exact unit reconstruction: units per SIP = amount / price-on-date, valued "
                  "at the latest available price. Values are as of each holding's price_date.",
    }


def _portfolio_xirr(holdings: list[dict], rows: list[dict]) -> "float | None":
    """Whole-portfolio XIRR: pool every priced holding's SIP outflows with its current value
    (dated to its price_date). Pure metrics.xirr does the solve. None if not computable."""
    cashflows = []
    for h, r in zip(holdings, rows):
        if r.get("status") != "priced":
            continue
        pdate = date.fromisoformat(r["price_date"])
        for (y, m, amt) in h["inflows"]:
            if date(y, m, 1) <= pdate:
                cashflows.append((date(y, m, 1), -amt))
        cashflows.append((pdate, r["current_value"]))
    if len(cashflows) < 2:
        return None
    try:
        return round(metrics.xirr(cashflows) * 100, 2)
    except ValueError:
        return None


def fetch_snapshot(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, skipped = read_portfolio(path)
    rows = [reconstruct_holding(h) for h in holdings]
    snap = aggregate_snapshot(rows)
    snap["portfolio_xirr_pct"] = _portfolio_xirr(holdings, rows)
    if skipped:
        snap["skipped_rows"] = skipped
    return json.dumps(snap)


def fetch_holding_units(symbol: str, path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, _ = read_portfolio(path)
    code = str(symbol).strip()
    match = [h for h in holdings if h["symbol"] and str(h["symbol"]).lower() == code.lower()]
    if not match:
        raise ValueError(f"No holding with symbol '{code}' found in the portfolio.")
    return json.dumps(reconstruct_holding(match[0], include_breakdown=True))


def fetch_portfolio_xirr(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, _ = read_portfolio(path)
    rows = [reconstruct_holding(h) for h in holdings]
    per_holding = [{"name": r["name"], "symbol": r["symbol"], "xirr_pct": r.get("xirr_pct")}
                   for r in rows if r.get("status") == "priced"]
    return json.dumps({
        "portfolio_xirr_pct": _portfolio_xirr(holdings, rows),
        "per_holding": per_holding,
        "priced_count": len(per_holding),
        "note": "XIRR is the money-weighted annualized return. Portfolio XIRR pools every "
                "priced SIP cashflow; per-holding XIRR uses that holding's own cashflows.",
    })


def fetch_beta(symbol: str, period: str = "2y") -> str:
    """Beta = OLS slope of the holding's daily returns on NIFTY 50 daily returns."""
    code = str(symbol).strip()
    if code.isdigit():  # numeric AMFI code -> a mutual fund
        return json.dumps({
            "symbol": code, "beta": None, "applicable": False,
            "reason": "Beta versus an equity index is not applicable to mutual funds. "
                      "Provide a stock/ETF ticker (e.g. RELIANCE.NS).",
        })
    stock_hist = yf.Ticker(code).history(period=period)
    if stock_hist.empty:
        raise ValueError(f"No price data for '{code}' — check the ticker symbol.")
    stock = stock_hist["Close"]
    market = yf.Ticker(NIFTY50_SYMBOL).history(period=period)["Close"]
    if market.empty:
        raise ValueError("No data for the NIFTY 50 index.")
    joined = pd.concat([stock.rename("s"), market.rename("m")], axis=1, join="inner").dropna()
    rets = joined.pct_change().dropna()
    if len(rets) < 30:
        raise ValueError("Not enough overlapping trading days to estimate beta.")
    beta = metrics.regression_slope(rets["s"].tolist(), rets["m"].tolist())
    return json.dumps({
        "symbol": code, "applicable": True, "beta": round(beta, 3),
        "benchmark": "NIFTY 50", "period": period, "n_obs": len(rets),
        "note": "Beta of the daily returns vs NIFTY 50: ~1 moves with the market, >1 more "
                "volatile than the market, <1 less volatile. Historical, not a prediction.",
    })


def _holding_series_and_value(h: dict, lookback_days: int = 370):
    """For Sharpe: reconstruct current value and return the last ~1y of closes/NAVs as a
    date-indexed Series, from a single price fetch. (None, None) if not usable."""
    if h["source"] in ("", "manual") or not h["symbol"] or not h["inflows"]:
        return None, None
    start = date(h["inflows"][0][0], h["inflows"][0][1], 1) - timedelta(days=15)
    today = date.today()
    pmap = _price_map(h["source"], h["symbol"], start, today)
    if not pmap:
        return None, None
    latest = pmap[max(pmap)]
    priced = [{"date": date(y, m, 1), "amount": amt,
               "price": metrics.nav_on_or_before(pmap, date(y, m, 1))}
              for (y, m, amt) in h["inflows"]]
    value = metrics.reconstruct_units(priced)["total_units"] * latest
    cutoff = today - timedelta(days=lookback_days)
    s = pd.Series({pd.Timestamp(d): p for d, p in pmap.items() if d >= cutoff}).sort_index()
    return value, s


def fetch_sharpe(path: str = DEFAULT_PORTFOLIO) -> str:
    """Portfolio Sharpe: value-weighted daily returns over the common daily-data window,
    annualized (252 days), minus the manually-set risk-free rate, over annualized volatility.
    Holdings with under ~150 days of history are excluded; coverage is reported."""
    holdings, _ = read_portfolio(path)
    values, series = {}, {}
    for i, h in enumerate(holdings):
        try:
            v, s = _holding_series_and_value(h)
        except Exception:
            v, s = None, None
        # Require a substantial history (>= ~150 trading days) so newly-listed holdings
        # don't shrink the common-date window every series must share.
        if v and s is not None and len(s) >= 150:
            values[i], series[i] = v, s
    if len(series) < 2:
        raise ValueError("Not enough holdings with a full year of history to compute Sharpe.")
    rets = pd.DataFrame({i: series[i] for i in series}).pct_change().dropna()
    if len(rets) < 30:
        raise ValueError("Not enough overlapping trading days to compute Sharpe.")
    total = sum(values.values())
    weights = {i: values[i] / total for i in values}
    port_daily = sum(rets[i] * weights[i] for i in values)
    ann = metrics.annualize(port_daily.tolist())
    sharpe = metrics.sharpe_ratio(ann["ann_return"], ann["ann_volatility"], RISK_FREE_RATE)
    return json.dumps({
        "sharpe_ratio": round(sharpe, 3),
        "ann_return_pct": round(ann["ann_return"] * 100, 2),
        "ann_volatility_pct": round(ann["ann_volatility"] * 100, 2),
        "risk_free_rate_pct": round(RISK_FREE_RATE * 100, 2),
        "trading_days": ann["n_obs"],
        "holdings_covered": len(values),
        "value_covered": round(total, 2),
        "assumptions": "Value-weighted daily returns over the largest window of daily data "
                       "common to all covered holdings (see trading_days), annualized at 252 "
                       "trading days; risk-free = RBI 10Y G-Sec (manually set constant). "
                       "Holdings without >=150 days of daily history are excluded — see "
                       "holdings_covered and value_covered for coverage.",
    })


def fetch_concentration(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, _ = read_portfolio(path)
    rows = [reconstruct_holding(h) for h in holdings]
    priced = [{"name": r["name"], "value": r["current_value"], "market": r["market"],
               "type": r["type"], "risk": r["risk"]}
              for r in rows if r.get("status") == "priced"]
    if not priced:
        raise ValueError("No priced holdings available to measure concentration.")
    stats = metrics.concentration_stats(priced)
    stats["priced_count"] = len(priced)
    stats["total_count"] = len(rows)
    stats["note"] = ("Weights are by current value. HHI is the sum of squared fractional "
                     "weights (higher = more concentrated); effective_holdings = 1/HHI.")
    return json.dumps(stats)


def fetch_nav(fund_code: str) -> str:
    code = str(fund_code).strip()
    quote = _MF.get_scheme_quote(code)  # returns None for an unknown code
    if not quote:
        raise ValueError(
            f"No NAV found for fund code '{code}'. Check the AMFI scheme code."
        )
    return json.dumps({
        "fund_code": str(quote.get("scheme_code", code)),
        "scheme_name": quote.get("scheme_name"),
        "nav": float(quote["nav"]),
        "as_of": quote.get("last_updated"),
    })


def fetch_price_history(ticker: str, period: str, max_points: int = 30) -> str:
    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty:
        raise ValueError(f"No data for '{ticker}' — check the ticker symbol.")
    closes = hist["Close"]
    step = max(1, len(closes) // max_points)  # downsample long series to stay compact
    series = [{"date": str(idx.date()), "close": round(float(v), 2)}
              for idx, v in list(closes.items())[::step]]
    last = {"date": str(closes.index[-1].date()), "close": round(float(closes.iloc[-1]), 2)}
    if series[-1] != last:
        series.append(last)
    out = {
        "symbol": ticker,
        "period": period,
        "from": str(closes.index[0].date()),
        "to": str(closes.index[-1].date()),
        "trading_days": len(closes),
        "start_close": round(float(closes.iloc[0]), 2),
        "end_close": round(float(closes.iloc[-1]), 2),
        "high": round(float(closes.max()), 2),
        "low": round(float(closes.min()), 2),
        "series": series,
    }
    if step > 1:
        out["note"] = f"series downsampled to {len(series)} points from {len(closes)} trading days"
    return json.dumps(out)


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    try:
        if name == "get_price":
            return fetch_close(args["ticker"]), False
        if name == "get_index":
            return fetch_close(NIFTY50_SYMBOL), False
        if name == "get_return":
            return json.dumps(compute_return(args["ticker"], args["period"])), False
        if name == "compare_returns":
            return fetch_compare(args["ticker_a"], args["ticker_b"], args["period"]), False
        if name == "load_portfolio":
            return load_portfolio(), False
        if name == "portfolio_snapshot":
            return fetch_snapshot(), False
        if name == "get_nav":
            return fetch_nav(args["fund_code"]), False
        if name == "price_history":
            return fetch_price_history(args["ticker"], args["period"]), False
        if name == "portfolio_xirr":
            return fetch_portfolio_xirr(), False
        if name == "holding_units":
            return fetch_holding_units(args["symbol"]), False
        if name == "beta":
            return fetch_beta(args["symbol"]), False
        if name == "sharpe":
            return fetch_sharpe(), False
        if name == "concentration":
            return fetch_concentration(), False
        return f"Unknown tool: {name}", True
    except Exception as e:
        return f"Error: {e}", True


def turn(client: anthropic.Anthropic, messages: list, tools: list | None = None,
         tools_used: list | None = None) -> str:
    """Run the tool-calling loop until the model answers in text. `tools` defaults to the
    full tool set; pass [] for a no-tools turn (education). Tool names actually called are
    appended to `tools_used` if provided (used by the eval harness for tool-correctness)."""
    tools = TOOLS if tools is None else tools
    while True:
        kwargs = {"model": MODEL, "max_tokens": 1024, "system": SYSTEM, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return next((b.text for b in resp.content if b.type == "text"), "")
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                if tools_used is not None:
                    tools_used.append(block.name)
                content, is_error = run_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})


# Canned responses for the short-circuit categories (no model call, no tools).
OFFTOPIC_REDIRECT = (
    "That's outside what I do — I'm Portfolio X-Ray, an analytics assistant for your "
    "investments. Ask me about your portfolio's value, returns (XIRR), risk (beta, Sharpe, "
    "concentration), a fund's NAV, or a stock's price history."
)
NEWS_STUB = (
    "I can't pull news yet — a citable news tool is planned for a later version. For now I can "
    "help with analytics: your holdings' value, XIRR, beta, Sharpe, concentration, NAVs, and "
    "price history."
)


def answer_query(client: anthropic.Anthropic, messages: list, user_text: str,
                 state: dict | None = None) -> dict:
    """Route the query, then dispatch. `advice` and `offtopic` short-circuit before any tool
    runs; `news` returns a stub; `education` answers with no tools; `analytics` runs the tool
    loop. `state` tracks the consecutive-refusal streak so we harden the refusal on repeats.
    Returns {category, answer, tools_used, refused, subtype?}."""
    state = {} if state is None else state
    route = router.classify(client, user_text)
    category = route["category"]
    messages.append({"role": "user", "content": user_text})

    if category == "advice":
        state["advice_streak"] = state.get("advice_streak", 0) + 1
        ref = refusals.refuse(user_text, repeat=state["advice_streak"] - 1)
        messages.append({"role": "assistant", "content": ref["message"]})
        return {"category": category, "answer": ref["message"], "tools_used": [],
                "refused": True, "subtype": ref["subtype"]}

    state["advice_streak"] = 0  # any non-advice query resets the streak
    if category == "offtopic":
        messages.append({"role": "assistant", "content": OFFTOPIC_REDIRECT})
        return {"category": category, "answer": OFFTOPIC_REDIRECT, "tools_used": [],
                "refused": False}
    if category == "news":
        messages.append({"role": "assistant", "content": NEWS_STUB})
        return {"category": category, "answer": NEWS_STUB, "tools_used": [], "refused": False}

    tools_used: list = []
    tools = [] if category == "education" else TOOLS  # education answers without tools
    answer = turn(client, messages, tools=tools, tools_used=tools_used)
    return {"category": category, "answer": answer, "tools_used": tools_used,
            "refused": False}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console can't print ₹ by default
    load_dotenv()  # ANTHROPIC_API_KEY from .env
    client = anthropic.Anthropic()
    messages: list = []
    state: dict = {}
    print("Portfolio X-Ray agent — ask about your portfolio, prices, returns, risk "
          "(XIRR, beta, Sharpe, concentration), or fund NAVs (Ctrl+C to quit)")
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        result = answer_query(client, messages, user, state)
        print(result["answer"])


if __name__ == "__main__":
    main()
