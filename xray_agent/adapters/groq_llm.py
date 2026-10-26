"""LLMPort over Groq's OpenAI-compatible API — a second provider, and a test of the seam.

`ports/llm.py` says plainly that it is a test seam and not provider independence, because
`assistant_content` is handed back to the next request verbatim and its shape was
Anthropic's. This adapter is what that claim costs: the agent loop did not change by one
line, but everything below had to be translated.

What had to be translated, and it is worth knowing which of these bit:

  tool schemas   {name, description, input_schema}
                   -> {type: function, function: {name, description, parameters}}
  tool_choice    {type: tool, name: finish}
                   -> {type: function, function: {name: finish}}
  assistant turn Anthropic content BLOCKS -> an OpenAI message with a tool_calls array
  tool results   ONE user message holding N tool_result blocks
                   -> N separate messages with role "tool"
  arguments      a dict -> a JSON STRING, and back again on the way out
  errors         no is_error field exists, so a failed result is prefixed in the text
  stop reason    finish_reason "tool_calls"/"stop" -> "tool_use"/"end_turn"

Two things to know before trusting a run on this:

* Groq's free tier is rate limited (roughly 30 requests/minute at the time of writing, and
  it varies by model). One investigation makes 8-12 model calls, so wrap this in
  RetryingLLM or expect 429s. Check your own console for current limits.
* Free inference costs nothing, so cost_inr is 0 and the COST budget never trips. The step
  and wall-clock budgets become the only runaway guards. That is a real reduction in
  safety margin, not a free lunch.

Model IDs on Groq change often and the public write-ups disagree, so nothing is hardcoded
beyond an overridable default: set GROQ_MODEL, or call list_models() to see what your key
can actually reach.
"""
from __future__ import annotations

import json
import os

from ..ports.llm import LLMResponse, ToolCall

DEFAULT_GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


# --- translation (pure, so it is testable without a key) ------------------------------------

def to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t.get("description", ""),
                          "parameters": t.get("input_schema") or
                          {"type": "object", "properties": {}}}}
            for t in tools]


def to_openai_tool_choice(choice: dict | None):
    """Anthropic's forced-tool form -> OpenAI's. The budget wrap-up depends on this
    actually forcing `finish`; getting it wrong would silently let the model keep going."""
    if not choice:
        return None
    if choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    if choice.get("type") in ("any", "auto"):
        return "required" if choice["type"] == "any" else "auto"
    return None


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate the loop's Anthropic-shaped message list.

    The subtle one is tool results. Anthropic puts N tool_result blocks inside ONE user
    message; OpenAI wants N separate messages with role "tool". Collapsing them the wrong
    way makes the model lose track of which result answered which call, which shows up as
    findings citing the wrong evidence ID rather than as an obvious crash.
    """
    out: list[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content")

        if role == "assistant":
            # GroqLLM stores assistant_content as the OpenAI message BODY (no role).
            body = content if isinstance(content, dict) else {"content": str(content)}
            msg = {"role": "assistant", "content": body.get("content")}
            if body.get("tool_calls"):
                msg["tool_calls"] = body["tool_calls"]
            out.append(msg)
            continue

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = str(block.get("content", ""))
                    # OpenAI tool messages have no is_error flag, so say it in the text or
                    # the model reads a rejection as a normal result and moves on.
                    if block.get("is_error") and not text.lstrip().upper().startswith(
                            ("ERROR", "FINDING REJECTED")):
                        text = f"ERROR: {text}"
                    out.append({"role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
                                "content": text})
                elif isinstance(block, dict) and block.get("type") == "text":
                    out.append({"role": role or "user", "content": block.get("text", "")})
            continue

        out.append({"role": role or "user", "content": content})
    return out


def from_openai_response(resp, model: str) -> LLMResponse:
    choice = resp.choices[0]
    msg = choice.message
    raw_calls = list(getattr(msg, "tool_calls", None) or [])

    tool_calls, serialised = [], []
    for tc in raw_calls:
        raw_args = getattr(tc.function, "arguments", "") or "{}"
        try:
            args = json.loads(raw_args)
        except (ValueError, TypeError):
            # A model that emits malformed JSON must not take the run down. An empty dict
            # reaches the loop, which validates and returns an actionable error.
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        serialised.append({"id": tc.id, "type": "function",
                           "function": {"name": tc.function.name, "arguments": raw_args}})

    body: dict = {"content": msg.content}
    if serialised:
        body["tool_calls"] = serialised

    usage = getattr(resp, "usage", None)
    return LLMResponse(
        stop_reason="tool_use" if tool_calls else "end_turn",
        text=msg.content or "",
        tool_calls=tool_calls,
        assistant_content=body,
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        model=model,
    )


# --- the adapter ----------------------------------------------------------------------------

class GroqLLM:
    def __init__(self, client=None, model: str = DEFAULT_GROQ_MODEL):
        self.model = model
        if client is not None:
            self.client = client
            return
        from groq import Groq  # raises if the extra is not installed

        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError("GROQ_API_KEY is not set (put it in .env)")
        self.client = Groq()

    def generate(self, *, system, messages, tools=None, max_tokens=1024, tool_choice=None):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + to_openai_messages(messages),
        }
        oa_tools = to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
            choice = to_openai_tool_choice(tool_choice)
            if choice:
                kwargs["tool_choice"] = choice
        return from_openai_response(self.client.chat.completions.create(**kwargs), self.model)

    def list_models(self) -> list[str]:
        """What this key can actually reach. Groq's free model list changes and the public
        write-ups disagree, so ask the API rather than trust a blog post."""
        try:
            return sorted(m.id for m in self.client.models.list().data)
        except Exception as e:
            return [f"<could not list models: {e}>"]
