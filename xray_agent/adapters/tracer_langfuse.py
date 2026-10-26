"""Langfuse tracer. Redaction lives here, and nowhere else.

The core hands this adapter real values — holding names, rupee amounts, the model's actual
claims. What leaves the machine is this adapter's decision, and the default is that
**no content leaves at all**: tool names, evidence IDs, statuses, latencies, token counts,
costs, budgets and outcomes, but not the numbers or the names they describe. The README
promises "nothing stored", and a tracer that quietly shipped a user's holdings to a SaaS
would make that promise false.

    XRAY_TRACE_CONTENT=1    include inputs, outputs, claims and summaries (DEV ONLY)

Written against langfuse 5.0.4 (`start_observation` / `score_trace` / `flush`). Langfuse is
an OPTIONAL extra — it is deliberately not in requirements.txt, so the deployed Render app
never installs it and cannot be broken by it:

    uv sync --extra tracing

Every hook swallows its own exceptions. Observability must never be the thing that takes
an investigation down, so a broken trace degrades to a missing trace, never to a failed run.
"""
from __future__ import annotations

import os

REDACTED = "<redacted: set XRAY_TRACE_CONTENT=1 to include>"


def content_enabled() -> bool:
    return os.environ.get("XRAY_TRACE_CONTENT") == "1"


def _content(value):
    """Content passes through only when explicitly enabled. Default is a placeholder, not
    a partial mask — partial masking of financial text leaks more than people expect
    (a 'redacted' amount is still recoverable from a percentage and a total)."""
    return value if content_enabled() else REDACTED


class LangfuseTracer:
    """TracerPort that mirrors the console trace into Langfuse.

    Constructing it raises if keys are missing or the package is absent — the CLI catches
    that and falls back to the console tracer, which is the better demo surface anyway.
    """

    def __init__(self, fallback=None, client=None):
        self.fallback = fallback
        self.root = None
        self.spans: dict[str, object] = {}
        if client is not None:
            self.client = client
            return

        from langfuse import Langfuse  # raises if the extra is not installed

        pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = os.environ.get("LANGFUSE_SECRET_KEY")
        if not (pk and sk):
            raise RuntimeError("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
        self.client = Langfuse(
            public_key=pk, secret_key=sk,
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"))

    # --- plumbing -------------------------------------------------------------------------
    def _mirror(self, hook: str, *a, **kw) -> None:
        if self.fallback is not None:
            try:
                getattr(self.fallback, hook)(*a, **kw)
            except Exception:
                pass

    def _child(self, name: str, as_type: str = "span", **kw):
        parent = self.root or self.client
        try:
            return parent.start_observation(name=name, as_type=as_type, **kw)
        except Exception:
            return None

    @staticmethod
    def _end(span, **kw) -> None:
        if span is None:
            return
        try:
            if kw:
                span.update(**kw)
            span.end()
        except Exception:
            pass

    # --- hooks ----------------------------------------------------------------------------
    def run_start(self, state) -> None:
        self._mirror("run_start", state)
        try:
            self.root = self.client.start_observation(
                name="investigation", as_type="agent",
                input=_content(state.goal.question),
                metadata={"run_id": state.run_id,
                          "portfolio": os.path.basename(state.goal.portfolio_path),
                          "budget": state.budget.as_dict()})
        except Exception:
            self.root = None

    def plan_updated(self, state) -> None:
        self._mirror("plan_updated", state)
        self._end(self._child("plan", metadata={"steps": len(state.plan)},
                              output=_content([s.description for s in state.plan])))

    def llm_call(self, state, *, model, input_tokens, output_tokens, cost_inr,
                 latency_ms, stop_reason) -> None:
        self._mirror("llm_call", state, model=model, input_tokens=input_tokens,
                     output_tokens=output_tokens, cost_inr=cost_inr,
                     latency_ms=latency_ms, stop_reason=stop_reason)
        span = self._child("llm.generate", as_type="generation", model=model,
                           usage_details={"input": input_tokens, "output": output_tokens},
                           cost_details={"total": cost_inr},
                           metadata={"stop_reason": stop_reason, "latency_ms": latency_ms,
                                     "cost_inr": round(cost_inr, 6),
                                     "budget": state.budget_snapshot()})
        self._end(span)

    def tool_call(self, state, ev) -> None:
        self._mirror("tool_call", state, ev)
        span = self._child(
            f"tool.{ev.tool}", as_type="tool",
            input=_content(ev.args),
            output=_content(ev.result),
            level="ERROR" if ev.error else "DEFAULT",
            status_message=(_content(ev.error) if ev.error else None),
            metadata={"evidence_id": ev.id, "cache_hit": ev.cache_hit,
                      "retries": ev.retries, "latency_ms": ev.latency_ms,
                      "result_chars": len(ev.result), "ok": ev.ok})
        self._end(span)

    def verification(self, state, finding, ok, reason) -> None:
        self._mirror("verification", state, finding, ok, reason)
        span = self._child(
            "verify.finding", as_type="guardrail",
            input=_content(finding.claim),
            level="DEFAULT" if ok else "WARNING",
            status_message=(None if ok else _content(reason)),
            metadata={"verified": ok, "evidence_ids": finding.evidence_ids,
                      # The rejection CATEGORY is safe to export even when content is not:
                      # it is the thing you actually debug on, and it names no holding.
                      "rejection_kind": _rejection_kind(reason)})
        self._end(span)

    def note(self, state, message) -> None:
        self._mirror("note", state, message)
        self._end(self._child("note", metadata={"message": message}))

    def run_end(self, state) -> None:
        self._mirror("run_end", state)
        bs = state.budget_snapshot()
        scores = {
            "steps_used": bs["steps_used"],
            "cost_inr": bs["cost_inr"],
            "elapsed_s": bs["elapsed_s"],
            "findings_total": len(state.findings),
            "findings_verified": len(state.verified_findings),
            "findings_rejected": len(state.rejected_findings),
            "verified_ratio": round(state.verified_ratio(), 4),
            "evidence_count": len(state.ledger),
            "tool_errors": len(state.errors),
        }
        try:
            if self.root is not None:
                self.root.update(output=_content(state.summary),
                                 metadata={"outcome": state.outcome.value
                                           if state.outcome else None, **scores})
                for name, value in scores.items():
                    try:
                        self.root.score_trace(name=name, value=float(value))
                    except Exception:
                        pass
                try:
                    self.root.score_trace(
                        name="outcome",
                        value=state.outcome.value if state.outcome else "UNKNOWN",
                        data_type="CATEGORICAL")
                except Exception:
                    pass
                self.root.end()
        except Exception:
            pass
        finally:
            try:
                self.client.flush()
            except Exception:
                pass
            self.root = None


def _rejection_kind(reason: str | None) -> str | None:
    """Bucket a rejection so it is filterable in Langfuse without exporting the claim."""
    if not reason:
        return None
    r = reason.lower()
    if "no evidence cited" in r:
        return "uncited"
    if "unknown evidence" in r:
        return "bad_evidence_id"
    if "failed tool call" in r:
        return "cites_failed_tool"
    if "recommendation" in r:
        return "advice_language"
    if "not in the cited evidence" in r:
        return "unsourced_number"
    return "other"
