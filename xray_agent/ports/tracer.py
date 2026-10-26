"""The observability seam.

Every method is fire-and-forget: a tracer that raises must never take an investigation down
with it, so adapters swallow their own errors. Redaction is an adapter concern — the core
hands over real values and each tracer decides what it is allowed to emit.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TracerPort(Protocol):
    def run_start(self, state) -> None: ...

    def plan_updated(self, state) -> None: ...

    def llm_call(self, state, *, model: str, input_tokens: int, output_tokens: int,
                 cost_inr: float, latency_ms: int, stop_reason: str) -> None: ...

    def tool_call(self, state, evidence) -> None: ...

    def verification(self, state, finding, ok: bool, reason: str | None) -> None: ...

    def note(self, state, message: str) -> None: ...

    def run_end(self, state) -> None: ...


class NoopTracer:
    """Used by tests and by --trace none. Every hook is a no-op."""

    def run_start(self, state): pass
    def plan_updated(self, state): pass
    def llm_call(self, state, **kw): pass
    def tool_call(self, state, evidence): pass
    def verification(self, state, finding, ok, reason): pass
    def note(self, state, message): pass
    def run_end(self, state): pass
