"""Live console trace — the primary demo surface.

The point of this adapter is to make the agency *visible*. A chat box that returns an answer
looks like a chatbot however sophisticated the machinery; the same run printed as plan ->
tool calls -> evidence IDs -> accepted/rejected findings -> budget -> outcome is obviously an
agent, and you can watch it change its mind.

Everything is ASCII. The Windows console's default code page has no rupee glyph (agent.main
already works around this), and a demo that prints mojibake in front of an interviewer is a
self-inflicted wound.
"""
from __future__ import annotations

import sys

WIDTH = 78


def _ascii(s: str) -> str:
    """Make a string safe for a cp1252 console without losing meaning."""
    return (str(s).replace("₹", "Rs ").replace("→", "->").replace("—", "-")
            .replace("–", "-").replace("‘", "'").replace("’", "'")
            .replace("“", '"').replace("”", '"').replace("×", "x")
            .encode("ascii", "replace").decode("ascii"))


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


class ConsoleTracer:
    def __init__(self, stream=None, verbose: bool = False):
        self.out = stream or sys.stdout
        self.verbose = verbose
        self.n = 0

    # --- plumbing -------------------------------------------------------------------------
    def _p(self, line: str = "") -> None:
        try:
            print(_ascii(line), file=self.out)
        except Exception:
            pass   # a tracer must never take a run down with it

    def _rule(self, title: str = "") -> None:
        self._p("+" + ("-" * (WIDTH - 2)) + "+" if not title
                else "+-- " + _ascii(title) + " " + "-" * max(0, WIDTH - 6 - len(title)) + "+")

    def _step(self) -> str:
        self.n += 1
        return f"[{self.n:>2}]"

    # --- hooks ----------------------------------------------------------------------------
    def run_start(self, state) -> None:
        b = state.budget
        self._rule("INVESTIGATION")
        self._p(f"| goal      : {_clip(state.goal.question, WIDTH - 15)}")
        self._p(f"| portfolio : {state.goal.portfolio_path}")
        self._p(f"| budget    : {b.max_steps} steps | Rs {b.max_cost_inr:.2f} | "
                f"{b.max_seconds:.0f}s")
        self._rule()
        self._p()

    def plan_updated(self, state) -> None:
        self._p(f"{self._step()} PLAN")
        for i, s in enumerate(state.plan, 1):
            self._p(f"     {i}. {_clip(s.description, WIDTH - 20):<{WIDTH - 20}} [{s.status.value}]")
        self._p()

    def llm_call(self, state, *, model, input_tokens, output_tokens, cost_inr,
                 latency_ms, stop_reason) -> None:
        if self.verbose:
            self._p(f"     .. {model} {input_tokens}in/{output_tokens}out "
                    f"Rs {cost_inr:.4f} {latency_ms}ms ({stop_reason})")

    def tool_call(self, state, ev) -> None:
        args = _clip(", ".join(f"{k}={v}" for k, v in (ev.args or {}).items()), 44) or "-"
        self._p(f"{self._step()} TOOL  {ev.tool}({args})")
        if ev.error:
            tag = "transient" if getattr(ev, "transient", False) else "failed"
            self._p(f"     {ev.id}  [{tag}] {_clip(ev.error, WIDTH - 20)}")
        else:
            flags = []
            if ev.cache_hit:
                flags.append("cached")
            if ev.retries:
                flags.append(f"{ev.retries} retr{'y' if ev.retries == 1 else 'ies'}")
            extra = (" " + " ".join(f"[{f}]" for f in flags)) if flags else ""
            self._p(f"     {ev.id}  ok  {ev.latency_ms}ms  "
                    f"{len(ev.result)} chars{extra}")
        self._p()

    def verification(self, state, finding, ok, reason) -> None:
        mark = "ACCEPTED" if ok else "REJECTED"
        cites = ", ".join(finding.evidence_ids) or "nothing"
        self._p(f"{self._step()} FINDING [{mark}]  cites {cites}")
        self._p(f'     "{_clip(finding.claim, WIDTH - 10)}"')
        if not ok:
            self._p(f"     why: {_clip(reason, WIDTH - 12)}")
        self._p()

    def note(self, state, message) -> None:
        self._p(f"{self._step()} NOTE  {_clip(message, WIDTH - 12)}")
        self._p()

    def run_end(self, state) -> None:
        bs = state.budget_snapshot()
        self._rule(f"OUTCOME: {state.outcome.value if state.outcome else 'UNKNOWN'}")
        self._p(f"| findings  : {len(state.verified_findings)} verified, "
                f"{len(state.rejected_findings)} rejected")
        self._p(f"| evidence  : {len(state.ledger)} tool results")
        self._p(f"| spent     : {bs['steps_used']}/{bs['max_steps']} steps | "
                f"Rs {bs['cost_inr']:.4f} | {bs['elapsed_s']}s")
        if state.errors:
            self._p(f"| errors    : {len(state.errors)}")
        self._rule()
        self._p()
        if state.summary:
            self._p("SUMMARY")
            for line in state.summary.splitlines():
                self._p("  " + line)
            self._p()
        if state.verified_findings:
            self._p("VERIFIED FINDINGS")
            for f in state.verified_findings:
                self._p(f"  - {f.claim}  [{', '.join(f.evidence_ids)}]")
            self._p()
        if state.unresolved:
            self._p("COULD NOT DETERMINE")
            for u in state.unresolved:
                self._p(f"  - {u}")
            self._p()
        if state.open_questions:
            self._p("QUESTIONS FOR YOU")
            for q in state.open_questions:
                self._p(f"  - {q}")
            self._p()
