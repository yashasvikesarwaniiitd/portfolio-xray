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

MODEL = "claude-haiku-4-5"
NIFTY50_SYMBOL = "^NSEI"  # yfinance symbol for the NIFTY 50 index
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
    "Note on the portfolio snapshot: the CSV records money invested (SIP inflows), not units, so "
    "current values are ESTIMATES — units are approximated as invested ÷ average price over the "
    "accumulation window. Always tell the user these values are approximate. Holdings marked "
    "'price unavailable' (manual baskets, blank source, or newly-listed symbols with no data) "
    "have no live price — report them as such, never guess a value, and exclude them from totals."
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


def value_from_avg_cost(total_invested: float, avg_price: float,
                        current_price: float) -> dict:
    """Pure valuation math for the avg-cost approximation. Units are ESTIMATED as
    invested / average price over the accumulation window, then valued at today's price.
    No network I/O — unit-testable."""
    units = total_invested / avg_price
    current_value = units * current_price
    pnl = current_value - total_invested
    return {
        "units_est": round(units, 4),
        "avg_price_est": round(avg_price, 4),
        "current_price": round(current_price, 4),
        "invested": round(total_invested, 2),
        "current_value_est": round(current_value, 2),
        "pnl_abs_est": round(pnl, 2),
        "pnl_pct_est": round(pnl / total_invested * 100, 2) if total_invested else 0.0,
    }


def aggregate_snapshot(rows: list[dict]) -> dict:
    """Pure aggregation over per-holding results. Totals include only 'priced' holdings;
    'unavailable' holdings are reported and their invested capital summed separately.
    No network I/O — unit-testable."""
    priced = [r for r in rows if r.get("status") == "priced"]
    total_invested_priced = sum(r["invested"] for r in priced)
    total_current = sum(r["current_value_est"] for r in priced)
    total_invested_all = sum(r.get("invested", 0.0) for r in rows)
    total_pnl = total_current - total_invested_priced
    return {
        "holdings": rows,
        "priced_count": len(priced),
        "total_count": len(rows),
        "total_invested_priced": round(total_invested_priced, 2),
        "total_current_value_est": round(total_current, 2),
        "total_pnl_abs_est": round(total_pnl, 2),
        "total_pnl_pct_est": round(total_pnl / total_invested_priced * 100, 2)
                             if total_invested_priced else 0.0,
        "unpriced_invested": round(total_invested_all - total_invested_priced, 2),
        "note": "Current values are ESTIMATES: units approximated as invested / average "
                "price over the accumulation window, valued at today's price.",
    }


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


def _next_month_start(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def _price_series(source: str, symbol: str, start: date, end: date) -> "pd.Series | None":
    """Fetch a date-indexed closing-price/NAV series for [start, end], or None if no data.
    yfinance for stocks/ETFs and crypto pairs; mftool historical NAV for mutual funds."""
    if source in ("yfinance", "crypto"):
        hist = yf.Ticker(symbol).history(start=start.isoformat(),
                                         end=(end + timedelta(days=1)).isoformat())
        return None if hist.empty else hist["Close"]
    if source == "mftool":
        raw = _MF.get_scheme_historical_nav(str(symbol))
        if not raw or not raw.get("data"):
            return None
        dates, navs = [], []
        for d in raw["data"]:
            try:
                dates.append(pd.Timestamp(datetime.strptime(d["date"], "%d-%m-%Y")))
                navs.append(float(d["nav"]))
            except (ValueError, KeyError, TypeError):
                continue
        if not dates:
            return None
        s = pd.Series(navs, index=dates).sort_index().loc[
            pd.Timestamp(start):pd.Timestamp(end)]
        return s if len(s) else None
    return None


def price_holding(h: dict) -> dict:
    """Value one holding via the avg-cost approximation, routing by its source. Catches its
    own errors and returns a 'price unavailable' row rather than raising — one bad holding
    must never crash the whole snapshot."""
    base = {
        "name": h["name"], "symbol": h["symbol"], "source": h["source"] or "(blank)",
        "type": h["type"], "invested": round(h["total_invested"], 2),
    }
    if h["source"] in ("", "manual") or not h["symbol"]:
        return {**base, "status": "unavailable",
                "reason": "no live-price source (manual basket or blank source)"}
    if not h["inflows"]:
        return {**base, "status": "unavailable",
                "reason": "no recorded SIP inflows to estimate units from"}
    try:
        first_y, first_m, _ = h["inflows"][0]
        last_y, last_m, _ = h["inflows"][-1]
        start = date(first_y, first_m, 1)
        today = date.today()
        series = _price_series(h["source"], h["symbol"], start, today)
        if series is None or len(series) == 0:
            return {**base, "status": "unavailable",
                    "reason": "no price data for this period (possibly newly listed)"}
        current_price = float(series.iloc[-1])
        # Average over the accumulation window only (first inflow -> end of last inflow month).
        acc_upper = min(_next_month_start(last_y, last_m), today + timedelta(days=1))
        idx_dates = [ts.date() for ts in series.index]
        acc = [v for d, v in zip(idx_dates, series.values) if d < acc_upper]
        avg_price = float(sum(acc) / len(acc)) if acc else float(series.mean())
        if avg_price <= 0:
            return {**base, "status": "unavailable", "reason": "non-positive average price"}
        vals = value_from_avg_cost(h["total_invested"], avg_price, current_price)
        return {**base, "status": "priced", "price_as_of": str(series.index[-1].date()),
                **vals}
    except Exception as e:  # any provider hiccup -> unavailable, never crash the snapshot
        return {**base, "status": "unavailable", "reason": f"price fetch failed: {e}"}


def fetch_snapshot(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, skipped = read_portfolio(path)
    rows = [price_holding(h) for h in holdings]
    snap = aggregate_snapshot(rows)
    if skipped:
        snap["skipped_rows"] = skipped
    return json.dumps(snap)


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
        return f"Unknown tool: {name}", True
    except Exception as e:
        return f"Error: {e}", True


def turn(client: anthropic.Anthropic, messages: list) -> str:
    while True:
        resp = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM,
                                      tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return next((b.text for b in resp.content if b.type == "text"), "")
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                content, is_error = run_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console can't print ₹ by default
    load_dotenv()  # ANTHROPIC_API_KEY from .env
    client = anthropic.Anthropic()
    messages = []
    print("Portfolio X-Ray agent — ask about your portfolio, prices, returns, "
          "history, indices, or fund NAVs (Ctrl+C to quit)")
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        print(turn(client, messages))


if __name__ == "__main__":
    main()
