# Portfolio X-Ray

An agentic portfolio analytics assistant: ingests a stock/MF portfolio, fetches
live market data (yfinance for prices/sector data, AMFI/mftool for MF NAVs,
Google News RSS for citable news), computes risk metrics (beta, XIRR, Sharpe
using RBI 10Y G-Sec yield, concentration, fund overlap on ISIN from full SEBI
monthly disclosures)
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
sheet. Each can arrive as CSV or as an Excel workbook (.xlsx/.xlsm): `excel_import`
turns a workbook into the same text a CSV export would be, so nothing downstream knows
the difference. Every sheet that parses as a portfolio becomes a part; the rest are
listed as skipped, never silently dropped. Excel turns the header `Mar'25` into a real
date, so on a SIP header row dates are written back as month labels. A statement is preferred — units are read from real fills rather than
reconstructed, and it can express SELLS, which the SIP sheet cannot.
- Non-executed orders (cancelled/rejected/failed) are excluded and counted. An
  unrecognised status is KEPT and surfaced — dropping a real trade silently is worse.
- Realised P&L is FIFO (Indian capital-gains basis). Sells with no matching buy are
  reported as unmatched, never given a zero cost basis.
- Sale proceeds are POSITIVE cashflows for XIRR.
- MF scheme names resolve to AMFI codes by splitting core/plan/option; contradicting the
  stated plan is penalised, never defaulted to Direct Growth. A name match is only a
  CANDIDATE: it is accepted only if the scheme's NAVs match the NAVs the statement itself
  recorded on its purchase dates (`fund_resolver.nav_match`). A fund that cannot be verified
  is left unpriced, never priced on a guess. A name-only match once priced a fund on a
  different (index) fund whose NAV was ~30x smaller and fabricated a ~96% loss on it.
  Candidates come from contiguous containment on the compacted name, so "Small Cap" and
  "SmallCap" are one string and an index fund that merely contains the words is excluded.
- One position per instrument, not per venue: statement rows are grouped by ISIN (falling
  back to the bare ticker), so an NSE and a BSE fill of one stock are a single holding priced
  once on NSE. The ISIN prefix also types it (INE = share, INF = ETF/fund).
- The headline P&L is UNREALISED (open lots at FIFO cost). Realised P&L from sales is
  reported beside it, never folded in or dropped. A fully sold holding is "closed", not
  "unpriced". Portfolio XIRR is built from every real dated flow, sales as inflows.
- The benchmark is like-for-like: the same dated flows replayed into NIFTY 50
  (`agent.index_equivalent_xirr`). Index levels (symbols starting `^`) are never converted
  to rupees - doing so once reported the index as rising over a window in which it fell.
- Cost uses the statement's amount paid when it agrees with units x price to within 1%.
- A portfolio is a sequence of PARTS (`portfolio_merge.PART_DELIM`), one per source file,
  read by their own parser and combined by `agent.read_portfolio`. Adding a file merges
  it: statement rows dedupe on a hash of all column values (multiset, so two identical
  fills in one file stay two trades); SIP sheets merge by holding identity, newer month
  figure wins. A file with no delimiter is a one-part portfolio and reads as before.
- Statement-sourced holdings arrive with no type or market; `infer_instrument` fills them
  from the venue suffix and price source. Without it they classify as "Other" and every
  stock-level section skips them.
- Saving is automatic when `SUPABASE_DB_URL` is set, never when it isn't, and never for the
  sample. Remove deletes the saved copy too, or it would return on refresh.

## Currency and benchmarks
- Portfolio maths is in INR. Dollar-quoted holdings (US tickers, crypto pairs) are
  converted in `_price_map` at each date's USD/INR rate - without it, units are
  `rupees / dollar price`, which is a ratio rather than a share count, and the currency
  move is invisible.
- Beta uses the venue's own index: NIFTY 50 for `.NS`/`.BO`, S&P 500 otherwise. Join the
  two series on calendar DATE, not the tz-aware timestamp, or nothing aligns across
  markets.

## Data sources (all free)
- Prices, indices, sector classification: yfinance
- Mutual fund NAVs: AMFI daily NAV feed via mftool
- Fund holdings (for overlap and look-through): each AMC's FULL monthly SEBI
  portfolio disclosure, collected out of band and committed as
  `data/fund_holdings_snapshot.json` with an as-of date and source URL per fund.
  Nothing fetches it at request time. Funds outside the snapshot fall back to
  yfinance top-10 names, and a fund with neither is reported as unknown — never
  as zero overlap. Refresh is manual, on the same monthly cadence as the G-Sec
  yield; see `data/FUND_HOLDINGS_SOURCES.md`.
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
