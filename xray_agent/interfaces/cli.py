"""Command line for the Investigator — the composition root and the demo surface.

    python -m xray_agent investigate "Why is my portfolio down this month?" \
        --portfolio portfolio.sample.csv --mode live --trace console

Modes differ only in where MARKET DATA comes from. The model's decisions are live in all
three, so every mode needs ANTHROPIC_API_KEY:

    live    tools hit yfinance / AMFI / Google News
    record  same, and every tool result is written to a fixture
    replay  tool results are served from a fixture; no market network at all

Replay is what makes the demo safe on bad wifi and what makes live evals repeatable — the
prices are frozen, the reasoning is not.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from ..adapters.anthropic_llm import DEFAULT_MODEL, AnthropicLLM
from ..adapters.legacy_tools import SCHEMAS, LegacyTools
from ..adapters.replay_tools import RecordingTools, ReplayTools
from ..adapters.sqlite_run_store import SqliteRunStore, recent_runs
from ..adapters.tracer_console import ConsoleTracer
from ..application.investigator import Investigator
from ..domain.models import Budget
from ..ports.tracer import NoopTracer

DEFAULT_FIXTURE_DIR = os.path.join("evals", "fixtures")


def _fixture_for(portfolio: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    stem = os.path.splitext(os.path.basename(portfolio))[0]
    return os.path.join(DEFAULT_FIXTURE_DIR, f"{stem}.tools.json")


def _build_tracer(kind: str, verbose: bool):
    if kind == "none":
        return NoopTracer()
    if kind == "langfuse":
        try:
            from ..adapters.tracer_langfuse import LangfuseTracer
            return LangfuseTracer(fallback=ConsoleTracer(verbose=verbose))
        except Exception as e:      # keys missing, package missing, anything
            print(f"[trace] langfuse unavailable ({e}); falling back to console",
                  file=sys.stderr)
            return ConsoleTracer(verbose=verbose)
    return ConsoleTracer(verbose=verbose)


def _build_tools(mode: str, portfolio: str, fixture: str, today: date):
    if mode == "replay":
        return ReplayTools(SCHEMAS, fixture)
    live = LegacyTools(portfolio, today=today)
    if mode == "record":
        return RecordingTools(live, fixture)
    return live


def run_investigate(args) -> int:
    import anthropic
    from dotenv import load_dotenv
    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (put it in .env). Every mode needs it: replay "
              "freezes market data, not the model.", file=sys.stderr)
        return 2

    today = date.fromisoformat(args.today) if args.today else date.today()
    fixture = _fixture_for(args.portfolio, args.fixture)
    try:
        tools = _build_tools(args.mode, args.portfolio, fixture, today)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    llm = AnthropicLLM(anthropic.Anthropic(), model=args.model)
    budget = Budget(max_steps=args.max_steps, max_cost_inr=args.max_cost,
                    max_seconds=args.max_seconds, max_tokens_per_call=args.max_tokens)

    agent = Investigator(llm, tools, tracer=_build_tracer(args.trace, args.verbose),
                         store=SqliteRunStore(), budget=budget, today=today)
    state = agent.investigate(args.question, args.portfolio)

    if args.mode == "record":
        tools.flush()
        print(f"[record] {len(tools.recorded)} tool results -> {fixture}")
    if args.mode == "replay" and getattr(tools, "misses", None):
        print(f"[replay] {len(tools.misses)} call(s) had no recorded result", file=sys.stderr)

    # Non-zero exit for anything that is not a clean, complete investigation, so scripts
    # and CI can tell the difference without parsing the trace.
    return 0 if state.outcome and state.outcome.value in ("COMPLETED", "REFUSED") else 1


def run_history(args) -> int:
    rows = recent_runs(args.limit)
    if not rows:
        print("No investigations recorded yet.")
        return 0
    print(f"{'when':<20} {'outcome':<18} {'steps':>5} {'Rs':>8} {'ok/total':>9}  question")
    for r in rows:
        print(f"{r['ts']:<20} {r['outcome'] or '-':<18} {r['steps_used']:>5} "
              f"{r['cost_inr']:>8.4f} "
              f"{str(r['findings_verified']) + '/' + str(r['findings_total']):>9}  "
              f"{(r['question'] or '')[:40]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m xray_agent",
        description="Portfolio X-Ray Investigator: a bounded, verified, traced agent.")
    sub = p.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("investigate", help="investigate a question about a portfolio")
    inv.add_argument("question")
    inv.add_argument("--portfolio", default="portfolio.sample.csv")
    inv.add_argument("--mode", choices=["live", "record", "replay"], default="live")
    inv.add_argument("--trace", choices=["console", "langfuse", "none"], default="console")
    inv.add_argument("--fixture", default=None,
                     help="replay/record fixture path (default: evals/fixtures/<name>.tools.json)")
    inv.add_argument("--model", default=DEFAULT_MODEL,
                     help="planner model (or set XRAY_PLANNER_MODEL)")
    inv.add_argument("--today", default=None,
                     help="override today's date (YYYY-MM-DD) for reproducible runs")
    inv.add_argument("--max-steps", type=int, default=12)
    inv.add_argument("--max-cost", type=float, default=10.0, help="rupees")
    inv.add_argument("--max-seconds", type=float, default=120.0)
    inv.add_argument("--max-tokens", type=int, default=1500)
    inv.add_argument("--verbose", action="store_true", help="show per-call token and cost lines")
    inv.set_defaults(func=run_investigate)

    hist = sub.add_parser("history", help="list recent investigations from SQLite")
    hist.add_argument("--limit", type=int, default=10)
    hist.set_defaults(func=run_history)
    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)
