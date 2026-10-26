"""Record replay fixtures for the Investigator, with NO model in the loop.

Why this exists rather than just running `--mode record` on a live investigation: recording
that way costs Anthropic credits and only captures the handful of calls that one run
happened to make. This drives the tool layer directly over a generous grid of plausible
calls, so it costs nothing but free market data, and a later replay run is far less likely
to miss.

    uv run python evals/record_fixtures.py                  # both demo portfolios
    uv run python evals/record_fixtures.py --portfolio portfolio.demo_b.csv

A replay miss is never silent, so topping a fixture up later with `--mode record` on a live
run is always safe.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
from xray_agent.adapters.legacy_tools import LegacyTools  # noqa: E402
from xray_agent.adapters.replay_tools import cache_key  # noqa: E402

PORTFOLIOS = ["portfolio.sample.csv", "portfolio.demo_b.csv"]
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def windows(today: date) -> list[tuple[date, date]]:
    """Windows a model plausibly resolves 'this month', 'last quarter', 'this year' into.
    Generous on purpose — a miss costs a demo, a spare entry costs a few KB."""
    out = [
        (today - timedelta(days=7), today),
        (today - timedelta(days=30), today),
        (today - timedelta(days=60), today),
        (today - timedelta(days=90), today),
        (today - timedelta(days=180), today),
        (today - timedelta(days=365), today),
        (_month_start(today), today),
        (_month_start(today, 1), today),
        (_month_start(today, 3), today),
        (_month_start(today, 6), today),
        (date(today.year, 1, 1), today),
        (_month_start(today, 1), _month_start(today) - timedelta(days=1)),
    ]
    seen, uniq = set(), []
    for s, e in out:
        if s < e and (s, e) not in seen:
            seen.add((s, e))
            uniq.append((s, e))
    return uniq


def planned_calls(path: str, today: date) -> list[tuple[str, dict]]:
    holdings, _ = agent.read_portfolio(path)
    tickers = [h["symbol"] for h in holdings
               if h["symbol"] and h["source"] in ("yfinance", "crypto")]
    names = [h["name"] for h in holdings]

    jobs: list[tuple[str, dict]] = [
        ("load_portfolio", {}),
        ("portfolio_snapshot", {}),
        ("portfolio_xirr", {}),
        ("concentration", {}),
        ("fund_overlap", {}),
        ("sharpe", {}),
    ]
    jobs += [("attribute_move", {"start": s.isoformat(), "end": e.isoformat()})
             for s, e in windows(today)]
    jobs += [("holding_units", {"symbol": n}) for n in names]
    jobs += [("beta", {"symbol": t}) for t in tickers]
    jobs += [("beta", {"symbol": t, "period": "2y"}) for t in tickers]
    jobs += [("price_history", {"ticker": t, "period": p})
             for t in tickers for p in ("1mo", "6mo")]
    jobs += [("compare_returns", {"ticker_a": t, "ticker_b": "^NSEI", "period": "6mo"})
             for t in tickers]
    jobs += [("get_news", {"query": n}) for n in names]
    return jobs


def record(path: str, today: date, out_dir: str = FIXTURE_DIR) -> str:
    tools = LegacyTools(path, today=today)
    jobs = planned_calls(path, today)
    recorded: dict[str, dict] = {}
    errors = 0

    print(f"\n{path}: {len(jobs)} calls (one shared price fetch, then pure computation)")
    for i, (name, args) in enumerate(jobs, 1):
        res = tools.execute(name, args)
        recorded[cache_key(name, args)] = {"content": res.content,
                                           "is_error": res.is_error,
                                           "transient": res.transient}
        if res.is_error:
            errors += 1
        flag = "ERR " if res.is_error else "ok  "
        detail = ", ".join(f"{k}={v}" for k, v in args.items())[:46]
        print(f"  [{i:>3}/{len(jobs)}] {flag}{name:<18} {detail}")

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    dest = os.path.join(out_dir, f"{stem}.tools.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(recorded, f, indent=2, sort_keys=True)
    size_kb = os.path.getsize(dest) / 1024
    print(f"  -> {dest}  ({len(recorded)} entries, {size_kb:.0f} KB, "
          f"{errors} recorded as errors)")
    print(f"  price fetch for the whole portfolio took {tools.ctx.fetch_seconds}s, once")
    return dest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--portfolio", action="append", default=None)
    p.add_argument("--today", default=None, help="YYYY-MM-DD (default: today)")
    args = p.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    for path in (args.portfolio or PORTFOLIOS):
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        try:
            record(path, today)
        except Exception as e:
            print(f"FAILED {path}: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
