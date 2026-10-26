# Portfolio X-Ray

An agentic portfolio analytics assistant: ingests a stock/MF portfolio, fetches
live market data (yfinance for prices/sector data, AMFI/mftool for MF NAVs,
Google News RSS for citable news), computes risk metrics (beta, XIRR, Sharpe
using RBI 10Y G-Sec yield, concentration, fund overlap via top-10 holdings)
via deterministic Python tools, and explains results with citations.

## Hard rules
- NEVER give buy/sell/hold advice or predictions. Analytics and education only.
  Advice-seeking queries get a spec'd refusal (SEBI IA boundary).
- The LLM never does financial arithmetic. Python computes; the model
  orchestrates tools and explains.
- Every metric tool is a pure function with unit tests before it becomes a tool.

## Portfolio ingestion
Three input formats, detected from file contents (`agent.read_portfolio` dispatches):
broker equity order statements, mutual fund statements, and the original monthly SIP
sheet. A statement is preferred — units are read from real fills rather than
reconstructed, and it can express SELLS, which the SIP sheet cannot.
- Non-executed orders (cancelled/rejected/failed) are excluded and counted. An
  unrecognised status is KEPT and surfaced — dropping a real trade silently is worse.
- Realised P&L is FIFO (Indian capital-gains basis). Sells with no matching buy are
  reported as unmatched, never given a zero cost basis.
- Sale proceeds are POSITIVE cashflows for XIRR.
- MF scheme names resolve to AMFI codes by splitting core/plan/option; contradicting the
  stated plan is penalised, never defaulted to Direct Growth.

## Data sources (all free)
- Prices, indices, sector classification: yfinance
- Mutual fund NAVs: AMFI daily NAV feed via mftool
- Fund holdings (for overlap): top-10 disclosed holdings per fund only —
  full-portfolio overlap needs scraping AMC factsheet PDFs, deferred as a
  stretch goal
- News for citations: Google News RSS (free, no key required)
- Risk-free rate (for Sharpe): RBI 10Y G-Sec yield, updated manually on a
  monthly cadence — no reliable free live API exists for this

## Stack
Python 3.12 + uv, Anthropic API (Haiku 5.5 everywhere: routing, judging, insights,
synthesis, and the Investigator's planner - overridable via XRAY_PLANNER_MODEL),
yfinance, mftool/AMFI, SQLite logging, FastAPI + Streamlit.

## Conventions
- Secrets in .env only (ANTHROPIC_API_KEY), never committed.
- Every production failure becomes a pair in golden_dataset.jsonl.
- Tag versions v0.0, v0.1... at each ship check.
- The Investigator (`xray_agent/`) follows the dependency rule: `domain/` imports no
  adapter and no SDK, so the whole agent loop stays testable with a scripted model.
  A finding reaches the user only if `verify_finding` passes - prompts are not a
  safety mechanism on their own. See ARCHITECTURE.md.
