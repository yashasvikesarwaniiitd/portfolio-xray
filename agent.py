"""Minimal agent loop: Anthropic Messages API + two yfinance tools."""
import json
import sys

import anthropic
import yfinance as yf
from dotenv import load_dotenv

MODEL = "claude-haiku-4-5"
NIFTY50_SYMBOL = "^NSEI"  # yfinance symbol for the NIFTY 50 index

SYSTEM = (
    "You are a portfolio analytics assistant. Analytics and education only — never give "
    "buy/sell/hold advice or predictions. Never do financial arithmetic yourself: only report "
    "numbers exactly as returned by your tools. If no tool provides a number, say you can't "
    "compute it rather than calculating or estimating."
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
    print("Portfolio X-Ray agent — ask about stock prices or the NIFTY 50 (Ctrl+C to quit)")
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
