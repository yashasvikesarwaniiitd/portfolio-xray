"""The LLM seam.

An honest note on scope: this is NOT a provider-independence abstraction. `assistant_content`
is passed back into the next request verbatim, so its shape is Anthropic's. The port exists
for one concrete reason — so the agent loop can be driven by a scripted fake in tests with no
network and no API key. Pretending it buys portability would be a lie told in code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    stop_reason: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-native assistant content, appended verbatim to the running message list.
    assistant_content: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Billed at different rates (reads 0.1x, writes 1.25x), so they cannot be folded into
    # input_tokens without mispricing every cached call.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use" and bool(self.tool_calls)


@runtime_checkable
class LLMPort(Protocol):
    def generate(self, *, system: str, messages: list, tools: list | None = None,
                 max_tokens: int = 1024, tool_choice: dict | None = None) -> LLMResponse:
        ...
