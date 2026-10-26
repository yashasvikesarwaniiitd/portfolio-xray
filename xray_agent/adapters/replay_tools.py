"""Record/replay wrapper around any ToolPort.

Two reasons this exists, and the second is the important one:

  1. A demo that depends on yfinance and AMFI being up, on a laptop on conference wifi, is a
     demo that fails in front of the person you are trying to impress. `--mode replay` runs
     the real agent loop against recorded tool output, with no network at all.
  2. Live evals over market data are not repeatable — prices move, so the same question gives
     a different answer tomorrow and an assertion that passed today fails on Monday for no
     good reason. Recorded fixtures make the tool layer deterministic while leaving the
     model's decisions genuinely live.

Note what is NOT faked: the model still chooses which tools to call, in what order, and the
loop still verifies and budgets. Only the market data is frozen.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..ports.tools import ToolResult


def cache_key(name: str, args: dict) -> str:
    """Stable identity for a call. Keys are sorted so argument order cannot split the cache."""
    return json.dumps({"tool": name, "args": args or {}}, sort_keys=True, default=str)


class RecordingTools:
    """Executes against the real port and writes every result to a fixture file."""

    def __init__(self, inner, fixture_path: str):
        self.inner = inner
        self.fixture_path = fixture_path
        self.recorded: dict[str, dict] = {}

    def schemas(self):
        return self.inner.schemas()

    def execute(self, name: str, args: dict) -> ToolResult:
        result = self.inner.execute(name, args)
        self.recorded[cache_key(name, args)] = {
            "content": result.content, "is_error": result.is_error,
            "transient": result.transient,
        }
        self.flush()
        return result

    def flush(self) -> None:
        Path(self.fixture_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.fixture_path, "w", encoding="utf-8") as f:
            json.dump(self.recorded, f, indent=2, sort_keys=True)


class ReplayTools:
    """Serves recorded results. A miss is an explicit, actionable error — never a silent
    fallthrough to the network, which would make a 'hermetic' run quietly live."""

    def __init__(self, schemas: list[dict], fixture_path: str):
        self._schemas = schemas
        self.fixture_path = fixture_path
        self.misses: list[str] = []
        if not os.path.exists(fixture_path):
            raise FileNotFoundError(
                f"No replay fixture at {fixture_path}. Record one first with --mode record.")
        with open(fixture_path, encoding="utf-8") as f:
            self.recorded = json.load(f)

    def schemas(self):
        return self._schemas

    def execute(self, name: str, args: dict) -> ToolResult:
        key = cache_key(name, args)
        hit = self.recorded.get(key)
        if hit is None:
            self.misses.append(key)
            return ToolResult(
                content=(f"Error: no recorded result for {name} with those arguments. This "
                         "run is in replay mode and cannot reach the network. Try a tool "
                         "or arguments that were recorded."),
                is_error=True, transient=False)
        return ToolResult(content=hit["content"], is_error=hit.get("is_error", False),
                          transient=hit.get("transient", False))


class FaultInjectingTools:
    """Wraps a port and fails named tools on purpose, to prove the loop recovers.

    `fail_tools` maps a tool name to the error message it should produce. Used by the fault
    injection eval: break `beta` and check the agent either routes around it or says plainly
    that it could not get the number — rather than inventing one.
    """

    def __init__(self, inner, fail_tools: dict[str, str], transient: bool = False):
        self.inner = inner
        self.fail_tools = fail_tools
        self.transient = transient
        self.injected: list[str] = []

    def schemas(self):
        return self.inner.schemas()

    def execute(self, name: str, args: dict) -> ToolResult:
        if name in self.fail_tools:
            self.injected.append(name)
            return ToolResult(content=f"Error: {self.fail_tools[name]}",
                              is_error=True, transient=self.transient)
        return self.inner.execute(name, args)
