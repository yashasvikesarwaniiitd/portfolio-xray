"""Domain models for an investigation run.

Pure data + tiny behaviour. No I/O, no SDK types, no adapter imports — this module is what
the loop, the tracer, the store and the tests all agree on, so it must stay dependency-free.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class RunOutcome(str, Enum):
    """How an investigation ended. Anything other than COMPLETED must be visible to the user —
    we never dress up a partial answer as a finished one."""
    COMPLETED = "COMPLETED"
    INCOMPLETE_BUDGET = "INCOMPLETE_BUDGET"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    DROPPED = "dropped"


@dataclass
class PlanStep:
    """One step of the model's own plan. The model owns the content; we only track status
    so the trace can show what it intended versus what it actually did."""
    description: str
    status: StepStatus = StepStatus.PENDING

    def as_dict(self) -> dict:
        return {"description": self.description, "status": self.status.value}


@dataclass
class Evidence:
    """One tool call and its result, addressable by a stable ID (E1, E2, ...).

    `numbers` is every numeral the result contained. A finding may only state numbers that
    appear here — that is the mechanical link between "the model said it" and "a tool
    computed it", and it is what makes `verified` mean something.
    """
    id: str
    tool: str
    args: dict
    result: str
    numbers: frozenset[float] = frozenset()
    ts: float = field(default_factory=time.time)
    cache_hit: bool = False
    retries: int = 0
    error: str | None = None
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self, include_result: bool = True) -> dict:
        d = {"id": self.id, "tool": self.tool, "args": self.args, "cache_hit": self.cache_hit,
             "retries": self.retries, "error": self.error, "latency_ms": self.latency_ms,
             "result_chars": len(self.result)}
        if include_result:
            d["result"] = self.result
        return d


@dataclass
class Finding:
    """A claim the model wants to ship, plus the evidence it rests on.

    `verified` is set only by domain.policies.verify_finding. An unverified finding never
    reaches the user as a fact — that is principle 2 (evidence or silence) in code.
    """
    claim: str
    evidence_ids: list[str]
    verified: bool = False
    rejection_reason: str | None = None

    def as_dict(self) -> dict:
        return {"claim": self.claim, "evidence_ids": list(self.evidence_ids),
                "verified": self.verified, "rejection_reason": self.rejection_reason}


@dataclass
class Budget:
    """Hard caps on an investigation. Exhausting any one of them forces a wrap-up turn and an
    INCOMPLETE_BUDGET outcome — the agent is allowed to run out of road, never to run away."""
    max_steps: int = 12
    max_cost_inr: float = 10.0
    max_seconds: float = 120.0
    max_tokens_per_call: int = 1500

    def as_dict(self) -> dict:
        return {"max_steps": self.max_steps, "max_cost_inr": self.max_cost_inr,
                "max_seconds": self.max_seconds, "max_tokens_per_call": self.max_tokens_per_call}


@dataclass
class Goal:
    """What the user asked, and the portfolio it is asked about."""
    question: str
    portfolio_path: str


@dataclass
class RunState:
    """Everything one investigation accumulates. The loop mutates this; the tracer and the
    store read it. Keeping it in one object is what makes a run inspectable after the fact."""
    goal: Goal
    budget: Budget = field(default_factory=Budget)
    plan: list[PlanStep] = field(default_factory=list)
    ledger: dict[str, Evidence] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps_used: int = 0
    cost_inr: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    outcome: RunOutcome | None = None
    summary: str = ""
    open_questions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    run_id: str = ""

    # --- evidence -------------------------------------------------------------------------
    def next_evidence_id(self) -> str:
        return f"E{len(self.ledger) + 1}"

    def add_evidence(self, ev: Evidence) -> Evidence:
        self.ledger[ev.id] = ev
        return ev

    # --- budget ---------------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> str | None:
        """Name of the first budget that is spent, or None. Checked before every model call."""
        if self.steps_used >= self.budget.max_steps:
            return f"step budget ({self.budget.max_steps} steps)"
        if self.cost_inr >= self.budget.max_cost_inr:
            return f"cost budget (Rs {self.budget.max_cost_inr:.2f})"
        if self.elapsed() >= self.budget.max_seconds:
            return f"time budget ({self.budget.max_seconds:.0f}s)"
        return None

    def budget_snapshot(self) -> dict:
        return {"steps_used": self.steps_used, "max_steps": self.budget.max_steps,
                "cost_inr": round(self.cost_inr, 4), "max_cost_inr": self.budget.max_cost_inr,
                "elapsed_s": round(self.elapsed(), 2), "max_seconds": self.budget.max_seconds}

    # --- results --------------------------------------------------------------------------
    @property
    def verified_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.verified]

    @property
    def rejected_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.verified]

    def verified_ratio(self) -> float:
        return len(self.verified_findings) / len(self.findings) if self.findings else 0.0
