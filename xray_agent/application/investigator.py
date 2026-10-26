"""The Investigator: a bounded, verified, traced agent loop.

What makes this an agent rather than the routed workflow it replaces: nothing here decides
the sequence of tool calls. The model does, turn by turn, from what the previous call
returned. The same question against two different portfolios produces different tool
sequences, because the evidence differs. What this code decides is everything the model
must NOT be trusted with:

    the gate        advice never reaches the loop at all (fails safe to refusal)
    the budget      steps, rupees and wall clock, checked before every model call
    the evidence    every tool result is recorded with an ID the model has to cite
    the verifier    a claim ships only if its numbers are in the evidence it cited
    the recovery    transient failures retry; permanent ones are reported so it re-plans
    the exit        running out of budget yields a marked partial answer, never a fake one

Deliberately written by hand with no agent framework. Every control-flow decision here is
one I can explain, which is the whole point of the exercise.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date

import logger
import refusals
import router

from ..domain import policies
from ..domain.models import (Budget, Evidence, Finding, Goal, PlanStep, RunOutcome,
                             RunState, StepStatus)
from ..ports.run_store import NoopRunStore
from ..ports.tracer import NoopTracer
from . import prompts
from .tool_registry import ToolRegistry

MAX_TOOL_RETRIES = 2
RETRY_BASE_DELAY = 0.5
LOOP_WARN_AT = 3


def cost_inr(model: str, input_tokens: int, output_tokens: int) -> float:
    """Price one model call in rupees, from the same table the query log uses. An unpriced
    model costs 0 rather than crashing — but it also means the budget cannot see it, so new
    models must be added to logger.PRICING deliberately, with the docs checked."""
    p = logger.PRICING.get(model)
    if not p:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]) * logger.USD_TO_INR


def _key(name: str, args: dict) -> str:
    return json.dumps({"tool": name, "args": args or {}}, sort_keys=True, default=str)


class Investigator:
    def __init__(self, llm, tools, *, tracer=None, store=None, budget: Budget | None = None,
                 today: date | None = None, max_retries: int = MAX_TOOL_RETRIES,
                 retry_base_delay: float = RETRY_BASE_DELAY, sleep=time.sleep):
        self.llm = llm
        self.registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self.tracer = tracer or NoopTracer()
        self.store = store or NoopRunStore()
        self.budget = budget or Budget()
        self.today = today or date.today()
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.sleep = sleep

    # -- public ----------------------------------------------------------------------------
    def investigate(self, question: str, portfolio_path: str) -> RunState:
        state = RunState(goal=Goal(question=question, portfolio_path=portfolio_path),
                         budget=self.budget, run_id=uuid.uuid4().hex[:12])
        self._memo: dict[str, str] = {}
        self._repeats: dict[str, int] = {}
        self._step_no = 0
        self.tracer.run_start(state)
        try:
            self._run(state)
        except Exception as e:                      # a crash is an outcome, not a traceback
            state.errors.append(f"loop failed: {e}")
            state.outcome = RunOutcome.FAILED
            if not state.summary:
                state.summary = (f"The investigation could not be completed: {e}. "
                                 "No findings are reported.")
        self.tracer.run_end(state)
        self.store.save_run(state)
        return state

    # -- the loop --------------------------------------------------------------------------
    def _run(self, state: RunState) -> None:
        route = self._classify(state, state.goal.question)
        if route["category"] == "advice":
            state.summary = refusals.refuse(state.goal.question)["message"]
            state.outcome = RunOutcome.REFUSED
            self.tracer.note(state, f"refused before any tool ran ({route['reason']})")
            self._record(state, "gate", {"category": "advice", "reason": route["reason"]})
            return
        if route["category"] == "offtopic":
            state.summary = prompts.OFFTOPIC_REDIRECT
            state.outcome = RunOutcome.REFUSED
            self.tracer.note(state, "off-topic; redirected before any tool ran")
            self._record(state, "gate", {"category": "offtopic"})
            return

        schemas = self.registry.schemas()
        messages = [{"role": "user", "content": prompts.goal_prompt(
            state.goal.question, self.today.isoformat(), state.goal.portfolio_path)}]

        spent = None
        while True:
            spent = state.exhausted()
            if spent:
                break
            resp = self._generate(state, messages, tools=schemas)
            state.steps_used += 1
            messages.append({"role": "assistant", "content": resp.assistant_content})

            if not resp.wants_tools:
                # The model answered in prose instead of calling finish. Accept the text, but
                # it still has to clear the guardrail like any other summary.
                state.summary = resp.text
                state.outcome = RunOutcome.COMPLETED
                break

            results, finished = self._handle_calls(state, resp.tool_calls)
            messages.append({"role": "user", "content": results})
            if finished:
                state.outcome = RunOutcome.COMPLETED
                break

        if state.outcome is None:
            self._wrap_up(state, messages, spent or "budget")

        state.summary = self._safe_summary(state, messages)

    # -- turns -----------------------------------------------------------------------------
    def _generate(self, state: RunState, messages: list, *, tools=None, tool_choice=None,
                  max_tokens: int | None = None):
        started = time.perf_counter()
        resp = self.llm.generate(system=prompts.SYSTEM, messages=messages, tools=tools,
                                 max_tokens=max_tokens or self.budget.max_tokens_per_call,
                                 tool_choice=tool_choice)
        latency = int((time.perf_counter() - started) * 1000)
        charged = self._charge(state, resp)
        self.tracer.llm_call(state, model=resp.model, input_tokens=resp.input_tokens,
                             output_tokens=resp.output_tokens, cost_inr=charged,
                             latency_ms=latency, stop_reason=resp.stop_reason)
        self._record(state, "llm", {"model": resp.model, "in": resp.input_tokens,
                                    "out": resp.output_tokens, "cost_inr": round(charged, 6),
                                    "latency_ms": latency, "stop_reason": resp.stop_reason,
                                    "budget": state.budget_snapshot()})
        return resp

    def _charge(self, state: RunState, resp) -> float:
        c = cost_inr(resp.model, resp.input_tokens, resp.output_tokens)
        state.cost_inr += c
        return c

    def _classify(self, state: RunState, question: str) -> dict:
        """The governance gate, run through the LLM port so it is testable without network.

        Reuses router.ROUTER_SYSTEM and router.CLASSIFY_TOOL verbatim — the classifier the
        product already ships and has eval numbers for — and keeps its fail-safe: anything
        unexpected is treated as advice, so the failure mode is an unnecessary refusal
        rather than an unguarded answer. Not counted against max_steps: it is the gate, not
        a step of the investigation. Its cost IS charged.
        """
        try:
            resp = self.llm.generate(
                system=router.ROUTER_SYSTEM,
                messages=[{"role": "user", "content": question}],
                tools=[router.CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "classify"},
                max_tokens=200)
            self._charge(state, resp)
            for call in resp.tool_calls:
                if call.name == "classify":
                    cat = call.args.get("category")
                    if cat in router.CATEGORIES:
                        return {"category": cat, "reason": call.args.get("reason", "")}
        except Exception as e:
            return {"category": "advice", "reason": f"router error, failing safe: {e}"}
        return {"category": "advice", "reason": "router could not classify; failing safe"}

    # -- tool calls ------------------------------------------------------------------------
    def _handle_calls(self, state: RunState, calls) -> tuple[list, bool]:
        results, finished = [], False
        for call in calls:
            if call.name == "update_plan":
                content, is_error = self._update_plan(state, call.args), False
            elif call.name == "record_finding":
                content, is_error = self._record_finding(state, call.args)
            elif call.name == "finish":
                content, is_error = self._finish(state, call.args), False
                finished = True
            else:
                content, is_error = self._domain_call(state, call.name, call.args)
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": content, "is_error": is_error})
        return results, finished

    def _domain_call(self, state: RunState, name: str, args: dict) -> tuple[str, bool]:
        key = _key(name, args)
        self._repeats[key] = self._repeats.get(key, 0) + 1

        if key in self._memo:
            ev = state.ledger[self._memo[key]]
            self.tracer.tool_call(state, Evidence(
                id=ev.id, tool=ev.tool, args=ev.args, result=ev.result, numbers=ev.numbers,
                cache_hit=True, error=ev.error, latency_ms=0))
            body = self._with_id(ev, "(cached — identical call already made this run)")
            if self._repeats[key] >= LOOP_WARN_AT:
                body += "\n\n" + prompts.LOOP_WARNING.format(
                    tool=name, n=self._repeats[key])
            return body, ev.error is not None

        res, retries, latency = self._execute_with_retry(name, args)
        ev = Evidence(
            id=state.next_evidence_id(), tool=name, args=args, result=res.content,
            numbers=frozenset(policies.json_numbers(res.content)),
            retries=retries, latency_ms=latency,
            error=res.content if res.is_error else None)
        state.add_evidence(ev)
        self._memo[key] = ev.id
        if res.is_error:
            state.errors.append(f"{name}: {res.content}")
        self.tracer.tool_call(state, ev)
        self._record(state, "tool", ev.as_dict(include_result=False))
        return self._with_id(ev), res.is_error

    @staticmethod
    def _with_id(ev: Evidence, note: str = "") -> str:
        """Hand the result back stamped with its evidence ID, because that ID is what the
        model must cite for any number it takes from this result."""
        head = f"Evidence ID: {ev.id}"
        if note:
            head += f"  {note}"
        return f"{head}\n{ev.result}"

    def _execute_with_retry(self, name: str, args: dict):
        """Retry only what is worth retrying. A permanent error comes straight back so the
        model can re-plan around it; a transient one gets up to max_retries with backoff."""
        total_ms, attempts = 0, 0
        while True:
            started = time.perf_counter()
            res = self.registry.execute(name, args)
            total_ms += int((time.perf_counter() - started) * 1000)
            if not (res.is_error and res.transient) or attempts >= self.max_retries:
                return res, attempts, total_ms
            attempts += 1
            self.sleep(self.retry_base_delay * (2 ** (attempts - 1)))

    # -- control tools ---------------------------------------------------------------------
    def _update_plan(self, state: RunState, args: dict) -> str:
        steps = [str(s) for s in (args.get("steps") or []) if str(s).strip()]
        if not steps:
            return "Plan not updated: steps was empty. Give 2-5 concrete steps."
        revision = bool(state.plan)
        state.plan = [PlanStep(description=s) for s in steps]
        self.tracer.plan_updated(state)
        self._record(state, "plan", {"steps": [s.as_dict() for s in state.plan],
                                     "revision": revision, "note": args.get("note", "")})
        return (f"Plan {'revised' if revision else 'set'} with {len(steps)} steps. "
                "Now gather the evidence for step 1.")

    def _record_finding(self, state: RunState, args: dict) -> tuple[str, bool]:
        finding = Finding(claim=str(args.get("claim", "")).strip(),
                          evidence_ids=[str(e).strip() for e in (args.get("evidence_ids") or [])])
        ok, reason = policies.verify_finding(finding, state.ledger)
        finding.verified = ok
        finding.rejection_reason = reason
        state.findings.append(finding)
        self.tracer.verification(state, finding, ok, reason)
        self._record(state, "verify", {"claim": finding.claim, "ok": ok, "reason": reason,
                                       "evidence_ids": finding.evidence_ids})
        if ok:
            return (f"Finding accepted and recorded ({len(state.verified_findings)} so far).",
                    False)
        # Returned as a tool ERROR so the model treats it as something to fix, not as news.
        return f"Finding REJECTED. {reason}", True

    def _finish(self, state: RunState, args: dict) -> str:
        state.summary = str(args.get("summary", "")).strip()
        state.open_questions = [str(q) for q in (args.get("open_questions") or [])]
        state.unresolved = [str(u) for u in (args.get("unresolved") or [])]
        for s in state.plan:
            if s.status == StepStatus.PENDING:
                s.status = StepStatus.DONE
        self._record(state, "finish", {"summary": state.summary,
                                       "open_questions": state.open_questions,
                                       "unresolved": state.unresolved})
        return "Investigation closed."

    # -- endgame ---------------------------------------------------------------------------
    def _wrap_up(self, state: RunState, messages: list, spent: str) -> None:
        """Budget is gone. Spend one more turn forcing a `finish` built from what is already
        verified, so the user gets a marked partial answer instead of a truncated one."""
        state.outcome = RunOutcome.INCOMPLETE_BUDGET
        self.tracer.note(state, f"out of {spent} — forcing wrap-up")
        messages.append({"role": "user",
                         "content": prompts.BUDGET_WRAP_UP.format(what=spent)})
        try:
            resp = self._generate(state, messages, tools=self.registry.schemas(),
                                  tool_choice={"type": "tool", "name": "finish"})
            for call in resp.tool_calls:
                if call.name == "finish":
                    self._finish(state, call.args)
                    return
            if resp.text:
                state.summary = resp.text
        except Exception as e:
            state.errors.append(f"wrap-up turn failed: {e}")

    def _safe_summary(self, state: RunState, messages: list) -> str:
        """Last gate before anything reaches the user. A summary that reads as advice gets
        one rewrite; if that still fails, we ship the verified findings as a fixed template.
        The user always gets something true, and never gets a recommendation."""
        summary = (state.summary or "").strip()
        if summary and not policies.violates_guardrail(summary):
            return summary

        if summary and policies.violates_guardrail(summary):
            self.tracer.note(state, "summary read as advice — regenerating once")
            try:
                messages = messages + [
                    {"role": "user", "content": prompts.REWRITE_SUMMARY + "\n\n" + summary}]
                resp = self._generate(state, messages, tools=None, max_tokens=700)
                rewritten = (resp.text or "").strip()
                if rewritten and not policies.violates_guardrail(rewritten):
                    return rewritten
            except Exception as e:
                state.errors.append(f"summary rewrite failed: {e}")
            self.tracer.note(state, "rewrite still read as advice — using the fixed template")

        return self._template_summary(state)

    @staticmethod
    def _template_summary(state: RunState) -> str:
        """Deterministic fallback. Nothing generated, nothing unverified."""
        verified = state.verified_findings
        if not verified:
            return ("No finding could be verified against the evidence gathered, so there is "
                    "nothing this investigation can state as fact. "
                    + (f"{len(state.rejected_findings)} claim(s) were rejected for citing "
                       "numbers that were not in the evidence."
                       if state.rejected_findings else ""))
        lines = ["Verified findings from this investigation:"]
        lines += [f"- {f.claim} [{', '.join(f.evidence_ids)}]" for f in verified]
        if state.unresolved:
            lines.append("")
            lines.append("Not determined:")
            lines += [f"- {u}" for u in state.unresolved]
        return "\n".join(lines)

    # -- persistence -----------------------------------------------------------------------
    def _record(self, state: RunState, kind: str, payload: dict) -> None:
        self._step_no += 1
        self.store.save_step(state.run_id, self._step_no, kind, payload)


def investigate(question: str, portfolio_path: str, *, llm, tools, **kw) -> RunState:
    """Convenience wrapper for one-shot use."""
    return Investigator(llm, tools, **kw).investigate(question, portfolio_path)
