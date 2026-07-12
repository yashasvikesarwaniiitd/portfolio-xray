# Portfolio X-Ray

An agentic portfolio analytics assistant: ingests a stock/MF portfolio, fetches
live market data (yfinance, AMFI), computes risk metrics (beta, XIRR, Sharpe,
concentration, fund overlap) via deterministic Python tools, and explains
results with citations.

## Hard rules
- NEVER give buy/sell/hold advice or predictions. Analytics and education only.
  Advice-seeking queries get a spec'd refusal (SEBI IA boundary).
- The LLM never does financial arithmetic. Python computes; the model
  orchestrates tools and explains.
- Every metric tool is a pure function with unit tests before it becomes a tool.

## Stack
Python 3.12 + uv, Anthropic API (Haiku 4.5 routing/judging, Sonnet 4.6
synthesis), yfinance, mftool/AMFI, SQLite logging. Later: FastAPI + Streamlit.

## Conventions
- Secrets in .env only (ANTHROPIC_API_KEY), never committed.
- Every production failure becomes a pair in golden_dataset.jsonl.
- Tag versions v0.0, v0.1... at each ship check.
