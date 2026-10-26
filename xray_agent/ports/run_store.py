"""Persistence seam for completed runs, so an investigation can be inspected without
Langfuse (or without any network at all). Like the tracer, it must never break a run."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RunStorePort(Protocol):
    def save_run(self, state) -> None: ...

    def save_step(self, run_id: str, step_no: int, kind: str, payload: dict) -> None: ...


class NoopRunStore:
    def save_run(self, state): pass
    def save_step(self, run_id, step_no, kind, payload): pass
