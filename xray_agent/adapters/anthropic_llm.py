"""LLMPort implementations: the real Anthropic client, and a scripted fake for tests.

ScriptedLLM is what makes the loop's behaviour — budget exhaustion, error recovery, finding
repair, loop detection — testable as pure logic. Those paths are the ones that matter most
and the ones a live test would exercise least reliably, so they are driven deterministically.
"""
from __future__ import annotations

import os

from ..ports.llm import LLMResponse, ToolCall

DEFAULT_MODEL = os.environ.get("XRAY_PLANNER_MODEL", "claude-haiku-5-5")


class AnthropicLLM:
    """Wraps anthropic.Anthropic. Token counts come back on the response so the loop can
    price each call and charge it against the budget."""

    def __init__(self, client, model: str = DEFAULT_MODEL, effort: str | None = None):
        self.client = client
        self.model = model
        # Haiku 5.5 runs adaptive thinking and defaults to effort "medium". Left as None
        # (the model's default) rather than forced down: tool-choice quality is what the
        # divergence result depends on, and that is not worth trading for a few paise.
        self.effort = effort

    def generate(self, *, system, messages, tools=None, max_tokens=1024, tool_choice=None):
        # Prompt caching. The render order is tools -> system -> messages, so ONE breakpoint
        # on the system block caches the tool schemas too. Together they are ~2,800 tokens
        # re-sent on every call of every turn, and on Haiku 5.5 the minimum cacheable
        # prefix is 512 tokens so they qualify. On Haiku 4.5 the minimum was 4,096 and this
        # would have silently done nothing - no error, just cache_read_input_tokens: 0.
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        resp = self.client.messages.create(**kwargs)

        calls = [ToolCall(id=b.id, name=b.name, args=dict(b.input))
                 for b in resp.content if b.type == "tool_use"]
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = getattr(resp, "usage", None)
        if resp.stop_reason == "refusal":
            # New on Haiku 5.5 and there is NO server-side fallback. Without this the loop
            # sees "not tool_use", takes the empty text as the answer, and reports a
            # confident blank - the worst way to fail.
            detail = getattr(getattr(resp, "stop_details", None), "category", None)
            raise RuntimeError(
                f"the model declined this request (safety category: {detail or 'unknown'})")
        return LLMResponse(
            stop_reason=resp.stop_reason,
            text=text,
            tool_calls=calls,
            assistant_content=resp.content,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            model=self.model,
        )


class ScriptedLLM:
    """Replays a fixed list of LLMResponses, one per generate() call.

    Entries may be LLMResponse objects or zero-argument callables (so a script can react to
    how far the loop has got). Running past the end raises rather than looping forever — a
    test that consumes more turns than it scripted has found a real bug, not a fixture gap.
    """

    def __init__(self, script: list, model: str = "scripted"):
        self.script = list(script)
        self.model = model
        self.calls: list[dict] = []   # what the loop asked for, for assertions

    def generate(self, *, system, messages, tools=None, max_tokens=1024, tool_choice=None):
        self.calls.append({"system": system, "messages": list(messages),
                           "tools": tools, "max_tokens": max_tokens,
                           "tool_choice": tool_choice})
        if not self.script:
            raise AssertionError(
                f"ScriptedLLM exhausted after {len(self.calls)} call(s): the loop asked for "
                "another turn than the test scripted.")
        nxt = self.script.pop(0)
        resp = nxt() if callable(nxt) else nxt
        if resp.model == "":
            resp.model = self.model
        return resp


# --- helpers for writing scripts readably --------------------------------------------------

def says(text: str, *, input_tokens: int = 100, output_tokens: int = 50) -> LLMResponse:
    """A plain text turn (the model is done calling tools)."""
    return LLMResponse(stop_reason="end_turn", text=text,
                       assistant_content=[{"type": "text", "text": text}],
                       input_tokens=input_tokens, output_tokens=output_tokens)


def calls(*specs, input_tokens: int = 100, output_tokens: int = 50) -> LLMResponse:
    """A tool-use turn. Each spec is (name, args) or (name, args, id)."""
    tool_calls, content = [], []
    for i, spec in enumerate(specs):
        name, args = spec[0], spec[1]
        cid = spec[2] if len(spec) > 2 else f"tu_{name}_{i}"
        tool_calls.append(ToolCall(id=cid, name=name, args=args))
        content.append({"type": "tool_use", "id": cid, "name": name, "input": args})
    return LLMResponse(stop_reason="tool_use", tool_calls=tool_calls,
                       assistant_content=content,
                       input_tokens=input_tokens, output_tokens=output_tokens)


def classifies(category: str, reason: str = "scripted") -> LLMResponse:
    """The router gate's turn. Every investigation starts with one, so every script does."""
    return calls(("classify", {"category": category, "reason": reason}),
                 input_tokens=40, output_tokens=15)
