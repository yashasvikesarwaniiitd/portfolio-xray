"""Portfolio X-Ray agent loop: Anthropic Messages API + yfinance/mftool tools."""
import json
import os
import sys

import anthropic
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from mftool import Mftool

MODEL = "claude-haiku-4-5"
NIFTY50_SYMBOL = "^NSEI"  # yfinance symbol for the NIFTY 50 index
DEFAULT_PORTFOLIO = "portfolio.csv"
PORTFOLIO_COLUMNS = ["ticker", "quantity", "buy_price", "buy_date"]

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
    "the figures are Indian Rupees (₹)."
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
        "description": "Load the user's portfolio from their CSV file (columns: ticker, "
                       "quantity, buy_price, buy_date) and list the holdings. Use this to see "
                       "what the user owns before answering portfolio questions. Does NOT fetch "
                       "current prices — use portfolio_snapshot for valuation and P&L.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "portfolio_snapshot",
        "description": "Value the user's entire portfolio: fetches the current price of every "
                       "holding and returns per-holding current value, invested amount, and "
                       "absolute + percentage profit/loss, plus portfolio-level totals. Use this "
                       "for questions like total value, total P&L, or unrealized gains. All math "
                       "is done in Python.",
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


def read_portfolio(path: str = DEFAULT_PORTFOLIO) -> tuple[list[dict], list[str]]:
    """Parse the portfolio CSV into holdings. Returns (holdings, skipped_row_notes).

    Pure of any network I/O so it can be unit-tested. Raises ValueError (never crashes)
    on a missing file, missing columns, or a file with no usable rows."""
    if not os.path.exists(path):
        raise ValueError(
            f"Portfolio file '{path}' not found. Create it with columns: "
            f"{', '.join(PORTFOLIO_COLUMNS)}."
        )
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise ValueError(f"Could not read '{path}' as CSV: {e}")
    df.columns = [str(c).strip().lstrip("﻿") for c in df.columns]
    missing = [c for c in PORTFOLIO_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Portfolio file is missing required column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(PORTFOLIO_COLUMNS)}."
        )
    holdings, skipped = [], []
    for i, row in df.iterrows():
        line = int(i) + 2  # +1 for header, +1 for 1-based line numbers
        try:
            ticker = str(row["ticker"]).strip()
            if not ticker or ticker.lower() == "nan":
                raise ValueError("empty ticker")
            qty = float(row["quantity"])
            buy_price = float(row["buy_price"])
            if qty <= 0 or buy_price <= 0:
                raise ValueError("quantity and buy_price must be positive numbers")
            holdings.append({
                "ticker": ticker,
                "quantity": qty,
                "buy_price": round(buy_price, 2),
                "buy_date": str(row["buy_date"]).strip(),
            })
        except (ValueError, TypeError) as e:
            skipped.append(f"line {line}: {e}")
    if not holdings:
        detail = "; ".join(skipped) if skipped else "file has no data rows"
        raise ValueError(f"No valid holdings found ({detail}).")
    return holdings, skipped


def compute_snapshot(holdings: list[dict], prices: dict[str, float]) -> dict:
    """Pure P&L math: given holdings and a {ticker: current_price} map, compute per-holding
    and portfolio-level value and profit/loss. No network I/O — unit-testable. Totals
    include only holdings that have a current price."""
    rows, total_invested, total_current = [], 0.0, 0.0
    for h in holdings:
        price = prices.get(h["ticker"])
        if price is None:
            rows.append({"ticker": h["ticker"], "error": "no current price available"})
            continue
        invested = h["quantity"] * h["buy_price"]
        current = h["quantity"] * price
        pnl = current - invested
        rows.append({
            "ticker": h["ticker"],
            "quantity": h["quantity"],
            "buy_price": round(h["buy_price"], 2),
            "current_price": round(price, 2),
            "invested": round(invested, 2),
            "current_value": round(current, 2),
            "pnl_abs": round(pnl, 2),
            "pnl_pct": round(pnl / invested * 100, 2),
        })
        total_invested += invested
        total_current += current
    total_pnl = total_current - total_invested
    return {
        "holdings": rows,
        "total_invested": round(total_invested, 2),
        "total_current_value": round(total_current, 2),
        "total_pnl_abs": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested else 0.0,
        "priced_holdings": sum(1 for r in rows if "error" not in r),
        "total_holdings": len(rows),
    }


def load_portfolio(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, skipped = read_portfolio(path)
    out = {"holdings": holdings, "count": len(holdings)}
    if skipped:
        out["skipped_rows"] = skipped
    return json.dumps(out)


def fetch_snapshot(path: str = DEFAULT_PORTFOLIO) -> str:
    holdings, skipped = read_portfolio(path)
    prices, price_errors = {}, {}
    for h in holdings:
        try:
            hist = yf.Ticker(h["ticker"]).history(period="5d")
            if hist.empty:
                price_errors[h["ticker"]] = "no price data — check the ticker symbol"
            else:
                prices[h["ticker"]] = float(hist["Close"].iloc[-1])
        except Exception as e:  # one bad ticker shouldn't sink the whole snapshot
            price_errors[h["ticker"]] = str(e)
    snap = compute_snapshot(holdings, prices)
    if skipped:
        snap["skipped_rows"] = skipped
    if price_errors:
        snap["price_errors"] = price_errors
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
