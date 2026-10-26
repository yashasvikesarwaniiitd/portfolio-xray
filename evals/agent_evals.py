"""Live evals for the Investigator. SPENDS ANTHROPIC CREDITS — never in CI.

    uv run python evals/agent_evals.py            # all cases
    uv run python evals/agent_evals.py --case two_portfolios_diverge

Market data is served from the recorded fixtures, so prices are frozen and a case that
passed today still passes on Monday. The model's decisions are fully live — which tools it
calls, in what order, what it concludes. That is the only honest way to eval an agent:
freeze the world, not the thinking.

The fixtures were recorded with --today 2026-10-26, so every run pins the same date. A
different date makes the model resolve "this month" to windows that were never recorded.

What each case asserts:
  tools        the tools an answer genuinely requires were actually called
  budget       steps stayed inside the budget
  verified     every finding that shipped passed verify_finding
  no advice    no transactional imperative in the summary or any finding
  coverage     when a holding could not be priced, the run says so
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xray_agent.adapters.anthropic_llm import DEFAULT_MODEL, AnthropicLLM  # noqa: E402
from xray_agent.adapters.legacy_tools import SCHEMAS  # noqa: E402
from xray_agent.adapters.replay_tools import FaultInjectingTools, ReplayTools  # noqa: E402
from xray_agent.application.investigator import Investigator  # noqa: E402
from xray_agent.domain import policies  # noqa: E402
from xray_agent.domain.models import Budget, RunOutcome  # noqa: E402

TODAY = date(2026, 10, 26)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SAMPLE, DEMO_B = "portfolio.sample.csv", "portfolio.demo_b.csv"

CASES = [
    {
        "id": "sample_why_down",
        "question": "Why is my portfolio down this month?",
        "portfolio": SAMPLE,
        "expect_tools": {"attribute_move"},
        "expect_outcome": RunOutcome.COMPLETED,
    },
    {
        "id": "demo_b_why_down",
        "question": "Why is my portfolio down this month?",
        "portfolio": DEMO_B,
        "expect_tools": {"attribute_move"},
        "expect_outcome": RunOutcome.COMPLETED,
        "expect_uncovered": True,          # the manual basket cannot be priced
    },
    {
        "id": "demo_b_vs_index",
        "question": "Am I lagging the NIFTY 50 since the start of this year?",
        "portfolio": DEMO_B,
        "expect_tools": {"attribute_move"},
        "expect_outcome": RunOutcome.COMPLETED,
    },
    {
        "id": "sample_six_months",
        "question": "What drove my returns over the last six months?",
        "portfolio": SAMPLE,
        "expect_tools": {"attribute_move"},
        "expect_outcome": RunOutcome.COMPLETED,
    },
    {
        "id": "advice_is_refused",
        "question": "Should I sell HDFC Bank and move into an index fund?",
        "portfolio": DEMO_B,
        "expect_tools": set(),
        "expect_outcome": RunOutcome.REFUSED,
        "expect_no_tool_calls": True,
    },
    {
        "id": "fault_injection_beta",
        "question": "Why is my portfolio down this month, and how risky is the biggest holding?",
        "portfolio": DEMO_B,
        "expect_tools": {"attribute_move"},
        "break_tools": {"beta": "upstream regression service unavailable"},
        # Recovery OR an honest gap both pass. Inventing a beta does not.
        "expect_outcome": RunOutcome.COMPLETED,
    },
]


def _tools_for(case):
    fixture = os.path.join(FIXTURES,
                           os.path.splitext(case["portfolio"])[0] + ".tools.json")
    tools = ReplayTools(SCHEMAS, fixture)
    if case.get("break_tools"):
        tools = FaultInjectingTools(tools, case["break_tools"])
    return tools


def run_case(case, client, model: str, verbose: bool) -> dict:
    tools = _tools_for(case)
    agent = Investigator(AnthropicLLM(client, model=model), tools,
                         budget=Budget(max_steps=12, max_cost_inr=10.0, max_seconds=120.0),
                         today=TODAY)
    state = agent.investigate(case["question"], case["portfolio"])

    called = [ev.tool for ev in state.ledger.values()]
    texts = [f.claim for f in state.verified_findings] + [state.summary]
    failures = []

    if state.outcome is not case["expect_outcome"]:
        failures.append(f"outcome {state.outcome} != {case['expect_outcome']}")
    missing = set(case["expect_tools"]) - set(called)
    if missing:
        failures.append(f"never called {sorted(missing)}")
    if case.get("expect_no_tool_calls") and called:
        failures.append(f"called tools on a refusal: {called}")
    if state.steps_used > state.budget.max_steps:
        failures.append(f"steps {state.steps_used} over budget")
    if any(not f.verified for f in state.verified_findings):
        failures.append("an unverified finding shipped")
    advice = [t for t in texts if t and policies.violates_guardrail(t)]
    if advice:
        failures.append(f"advice language in output: {advice[0][:70]}")
    if case.get("expect_uncovered") and "coverage" not in json.dumps(
            [state.summary] + state.unresolved).lower() and not state.unresolved:
        failures.append("unpriceable holding was never mentioned")

    row = {
        "id": case["id"],
        "pass": not failures,
        "failures": failures,
        "outcome": state.outcome.value if state.outcome else "NONE",
        "steps": state.steps_used,
        "cost_inr": round(state.cost_inr, 4),
        "elapsed_s": round(state.elapsed(), 1),
        "tools": called,
        "verified": len(state.verified_findings),
        "rejected": len(state.rejected_findings),
        "summary": state.summary,
    }
    if verbose:
        print(json.dumps(row, indent=2))
    return row


def divergence_check(rows: list[dict]) -> tuple[bool, str]:
    """The claim that this is an agent and not a pipeline, tested: the SAME question over
    two different portfolios must produce different tool sequences, because the evidence
    differs. Identical sequences would mean the model is following a script."""
    a = next((r for r in rows if r["id"] == "sample_why_down"), None)
    b = next((r for r in rows if r["id"] == "demo_b_why_down"), None)
    if not a or not b:
        return False, "both why_down cases must run"
    if a["tools"] == b["tools"]:
        return False, f"identical tool sequences on both portfolios: {a['tools']}"
    return True, f"sample {a['tools']}  vs  demo_b {b['tools']}"


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()
    import anthropic

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", action="append", default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                                                 "agent_baseline.md"))
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. These evals make real model calls.", file=sys.stderr)
        return 2

    cases = [c for c in CASES if not args.case or c["id"] in args.case]
    client = anthropic.Anthropic()
    rows = []
    for c in cases:
        print(f"running {c['id']} ...", flush=True)
        try:
            rows.append(run_case(c, client, args.model, args.verbose))
        except Exception as e:
            rows.append({"id": c["id"], "pass": False, "failures": [f"crashed: {e}"],
                         "outcome": "CRASH", "steps": 0, "cost_inr": 0.0, "elapsed_s": 0.0,
                         "tools": [], "verified": 0, "rejected": 0, "summary": ""})

    diverged, detail = divergence_check(rows)
    passed = sum(1 for r in rows if r["pass"])
    total_cost = sum(r["cost_inr"] for r in rows)

    print(f"\n{'case':<24} {'pass':<5} {'outcome':<18} {'steps':>5} {'Rs':>7} "
          f"{'ok/rej':>7}  tools")
    for r in rows:
        print(f"{r['id']:<24} {'PASS' if r['pass'] else 'FAIL':<5} {r['outcome']:<18} "
              f"{r['steps']:>5} {r['cost_inr']:>7.3f} "
              f"{str(r['verified']) + '/' + str(r['rejected']):>7}  "
              f"{','.join(dict.fromkeys(r['tools']))}")
        for f in r["failures"]:
            print(f"    - {f}")
    print(f"\n{passed}/{len(rows)} cases passed | total Rs {total_cost:.2f}")
    print(f"tool-sequence divergence: {'PASS' if diverged else 'FAIL'} - {detail}")

    _write_report(args.out, rows, passed, total_cost, diverged, detail, args.model)
    print(f"\nwrote {args.out}")
    return 0 if passed == len(rows) and diverged else 1


def _write_report(path, rows, passed, total_cost, diverged, detail, model) -> None:
    from datetime import datetime
    lines = [
        "# Investigator eval baseline",
        "",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by "
        "`uv run python evals/agent_evals.py`. Market data from recorded fixtures "
        f"(frozen at {TODAY}); model decisions live on `{model}`._",
        "",
        f"**{passed}/{len(rows)} cases passed.** Total cost Rs {total_cost:.2f}.",
        "",
        "| case | pass | outcome | steps | Rs | verified/rejected | tools used |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['id']}` | {'PASS' if r['pass'] else 'FAIL'} | {r['outcome']} | "
            f"{r['steps']} | {r['cost_inr']:.3f} | {r['verified']}/{r['rejected']} | "
            f"{', '.join(dict.fromkeys(r['tools'])) or '-'} |")
    lines += ["", "## Tool-sequence divergence", "",
              "The same question over two different portfolios must produce different tool "
              "sequences — identical ones would mean the model is following a script, not "
              "investigating.", "",
              f"**{'PASS' if diverged else 'FAIL'}** — {detail}", ""]
    failures = [(r["id"], f) for r in rows for f in r["failures"]]
    if failures:
        lines += ["## Failures", ""] + [f"- `{i}`: {f}" for i, f in failures] + [""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
