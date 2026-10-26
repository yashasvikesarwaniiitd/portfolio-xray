"""The tool seam.

`transient` is the interesting field. A rate-limit or a dropped socket deserves a retry; a
symbol that does not exist never will, however many times you ask. Conflating the two either
burns the budget on hopeless retries or gives up on a blip — so the adapter classifies the
failure and the loop acts on that classification.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    transient: bool = False


@runtime_checkable
class ToolPort(Protocol):
    def schemas(self) -> list[dict]:
        """Anthropic tool schemas for everything this port can execute."""
        ...

    def execute(self, name: str, args: dict) -> ToolResult:
        """Run one tool. Must never raise — a failure is a ToolResult with is_error set."""
        ...
