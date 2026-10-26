"""Hermetic tests for the Groq adapter and the LLM retry wrapper.

No groq package, no API key, no network — a fake shaped like the SDK's response exercises
every translation path. The translation layer is where the bugs actually live (a dict of
arguments becoming a JSON string, N tool_result blocks becoming N messages), and it is
fully testable offline, so there is no excuse for shipping it unverified.

The test that matters most is the last one: a complete investigation driven through
GroqLLM, asserting the agent loop needed no change at all to run on a second provider.
"""
import json
from datetime import date

import pytest

from xray_agent.adapters.groq_llm import (GroqLLM, from_openai_response, to_openai_messages,
                                          to_openai_tool_choice, to_openai_tools)
from xray_agent.adapters.retrying_llm import (RetryingLLM, is_retryable,
                                              retry_after_seconds)
from xray_agent.application.investigator import Investigator
from xray_agent.domain.models import RunOutcome
from xray_agent.ports.tools import ToolResult
from test_xray_agent_loop import WINDOW, FakeTools


# --- fakes shaped like the Groq SDK ---------------------------------------------------------

class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, id, name, arguments):
        self.id, self.type, self.function = id, "function", _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Usage:
    def __init__(self, p=100, c=50):
        self.prompt_tokens, self.completion_tokens = p, c


class _Resp:
    def __init__(self, message, usage=None):
        self.choices = [type("C", (), {"message": message})()]
        self.usage = usage or _Usage()


class FakeGroq:
    """Replays responses and records exactly what was sent, so the translation can be
    asserted on rather than assumed."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.sent.append(kw)
                if not outer.responses:
                    raise AssertionError("FakeGroq exhausted")
                return outer.responses.pop(0)

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _groq_calls(*specs):
    return _Resp(_Msg(tool_calls=[_TC(f"call_{i}", n, json.dumps(a))
                                  for i, (n, a) in enumerate(specs)]))


def _groq_says(text):
    return _Resp(_Msg(content=text))


# --- schema and tool_choice translation -----------------------------------------------------

def test_tool_schemas_are_translated_to_openai_shape():
    out = to_openai_tools([{"name": "finish", "description": "end it",
                            "input_schema": {"type": "object",
                                             "properties": {"summary": {"type": "string"}}}}])
    assert out == [{"type": "function",
                    "function": {"name": "finish", "description": "end it",
                                 "parameters": {"type": "object",
                                                "properties": {"summary": {"type": "string"}}}}}]
    assert to_openai_tools(None) is None
    assert to_openai_tools([]) is None


def test_forced_tool_choice_is_translated():
    """The budget wrap-up relies on this actually forcing `finish`. If it silently became
    None, a run that was out of budget would keep investigating."""
    assert to_openai_tool_choice({"type": "tool", "name": "finish"}) == {
        "type": "function", "function": {"name": "finish"}}
    assert to_openai_tool_choice(None) is None


# --- message translation ---------------------------------------------------------------------

def test_one_user_message_of_tool_results_becomes_several_tool_messages():
    """Anthropic puts N tool_result blocks in ONE user message; OpenAI wants N messages
    with role "tool". Collapsing them wrongly makes the model lose which result answered
    which call — that shows up as findings citing the wrong evidence, not as a crash."""
    out = to_openai_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": {"content": None, "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "attribute_move", "arguments": "{}"}},
            {"id": "b", "type": "function",
             "function": {"name": "concentration", "arguments": "{}"}}]}},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "RESULT A",
             "is_error": False},
            {"type": "tool_result", "tool_use_id": "b", "content": "RESULT B",
             "is_error": False}]},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool"]
    assert len(out[1]["tool_calls"]) == 2
    assert out[2] == {"role": "tool", "tool_call_id": "a", "content": "RESULT A"}
    assert out[3] == {"role": "tool", "tool_call_id": "b", "content": "RESULT B"}


def test_failed_tool_results_are_marked_since_openai_has_no_is_error():
    out = to_openai_messages([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "no such symbol",
         "is_error": True}]}])
    assert out[0]["content"].startswith("ERROR: ")


def test_an_already_marked_error_is_not_double_prefixed():
    out = to_openai_messages([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a",
         "content": "Finding REJECTED. These numbers are not in the cited evidence: 99.9",
         "is_error": True}]}])
    assert not out[0]["content"].startswith("ERROR: ERROR")
    assert out[0]["content"].startswith("Finding REJECTED")


# --- response parsing ---------------------------------------------------------------------

def test_tool_call_arguments_are_parsed_back_from_json():
    r = from_openai_response(_groq_calls(("attribute_move", {"start": "2026-09-01"})), "m")
    assert r.stop_reason == "tool_use"
    assert r.tool_calls[0].name == "attribute_move"
    assert r.tool_calls[0].args == {"start": "2026-09-01"}
    assert r.input_tokens == 100 and r.output_tokens == 50


def test_malformed_tool_arguments_do_not_crash_the_run():
    """Open models emit broken JSON more often than Claude does. An empty dict reaches the
    loop, which validates it and hands back an actionable error — far better than a
    traceback halfway through an investigation."""
    r = from_openai_response(_Resp(_Msg(tool_calls=[_TC("x", "finish", "{not json")])), "m")
    assert r.tool_calls[0].args == {}
    assert r.stop_reason == "tool_use"


def test_a_text_answer_maps_to_end_turn():
    r = from_openai_response(_groq_says("the portfolio fell"), "m")
    assert r.stop_reason == "end_turn"
    assert r.text == "the portfolio fell"
    assert r.tool_calls == []


# --- retry wrapper ---------------------------------------------------------------------------

class _RateLimit(Exception):
    status_code = 429


class _BadRequest(Exception):
    status_code = 400


def test_rate_limits_and_5xx_are_retryable_but_client_errors_are_not():
    assert is_retryable(_RateLimit())
    assert is_retryable(Exception("Error code: 503 - service unavailable"))
    assert is_retryable(TimeoutError("timed out"))
    assert not is_retryable(_BadRequest())
    assert not is_retryable(ValueError("model does not exist"))


def test_retry_honours_retry_after_when_the_provider_sends_one():
    exc = _RateLimit()
    exc.response = type("R", (), {"headers": {"retry-after": "7"}})()
    assert retry_after_seconds(exc) == 7.0


def test_the_wrapper_retries_a_429_then_succeeds():
    calls, slept = {"n": 0}, []

    class Flaky:
        model = "fake"

        def generate(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _RateLimit()
            return _groq_says("ok")

    llm = RetryingLLM(Flaky(), base_delay=1.0, sleep=slept.append)
    assert llm.generate(system="s", messages=[]).choices[0].message.content == "ok"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]          # exponential
    assert llm.retries == 2


def test_the_wrapper_gives_up_after_max_retries():
    class AlwaysLimited:
        model = "fake"

        def generate(self, **kw):
            raise _RateLimit()

    with pytest.raises(_RateLimit):
        RetryingLLM(AlwaysLimited(), max_retries=2, sleep=lambda _s: None).generate(
            system="s", messages=[])


def test_a_non_retryable_error_is_raised_immediately():
    slept = []

    class Bad:
        model = "fake"

        def generate(self, **kw):
            raise _BadRequest()

    with pytest.raises(_BadRequest):
        RetryingLLM(Bad(), sleep=slept.append).generate(system="s", messages=[])
    assert slept == []                   # no time wasted on a hopeless retry


# --- the whole loop, on a second provider ------------------------------------------------------

def test_a_full_investigation_runs_on_groq_with_no_change_to_the_loop():
    """The real test of the port seam. Same Investigator, same tools, same prompts — only
    the adapter differs. If this passes, "provider independence" stopped being a claim."""
    move = json.dumps({"change": -12450.5, "market_effect": -18200.0, "new_money": 5749.5})
    tools = FakeTools({"attribute_move": ToolResult(move)})
    client = FakeGroq([
        _groq_calls(("classify", {"category": "analytics", "reason": "asks why"})),
        _groq_calls(("update_plan", {"steps": ["Decompose the move"]})),
        _groq_calls(("attribute_move", WINDOW)),
        _groq_calls(("record_finding", {"claim": "The portfolio fell 12450.5.",
                                        "evidence_ids": ["E1"]})),
        _groq_calls(("finish", {"summary": "Market movement, not withdrawals.",
                                "unresolved": ["nothing further"]})),
    ])
    agent = Investigator(GroqLLM(client=client, model="llama-3.3-70b-versatile"), tools,
                         today=date(2026, 10, 26), sleep=lambda _s: None)
    state = agent.investigate("Why is my portfolio down?", "portfolio.demo_b.csv")

    assert state.outcome is RunOutcome.COMPLETED
    assert len(state.verified_findings) == 1
    assert state.summary == "Market movement, not withdrawals."

    # The gate must have gone out as a FORCED tool call, or advice could slip past it.
    assert client.sent[0]["tool_choice"] == {"type": "function",
                                             "function": {"name": "classify"}}
    # Tool results must have arrived as role "tool", correlated by id.
    roles = [m["role"] for m in client.sent[-1]["messages"]]
    assert "tool" in roles
    # Free inference is free, so the cost budget cannot be the guard here.
    assert state.cost_inr == 0.0


def test_advice_is_still_refused_on_groq_before_any_tool_runs():
    tools = FakeTools({"attribute_move": ToolResult("{}")})
    client = FakeGroq([_groq_calls(("classify", {"category": "advice", "reason": "sell?"}))])
    agent = Investigator(GroqLLM(client=client, model="m"), tools,
                         today=date(2026, 10, 26), sleep=lambda _s: None)
    state = agent.investigate("Should I sell HDFC Bank?", "portfolio.demo_b.csv")

    assert state.outcome is RunOutcome.REFUSED
    assert tools.calls == []
    assert "not a SEBI-registered investment adviser" in state.summary


# --- failure modes found on the free tier --------------------------------------------------

_TPD = ("Error code: 429 - Rate limit reached for model `openai/gpt-oss-120b` on tokens "
        "per day (TPD): Limit 200000, Used 197148. Please try again in 8m25.872s")
_TOOL_JSON = ('Error code: 400 - Failed to parse tool call arguments as JSON, '
              'code: tool_use_failed, failed_generation: {"arguments": {"}')


def test_a_models_malformed_tool_json_is_retried_even_though_it_is_a_400():
    """Groq rejects the MODEL's bad tool JSON server-side with a 400, before any response
    reaches us — so the defensive parsing in from_openai_response never runs. A 400
    normally means we sent something wrong and retrying is pointless, but tool_use_failed
    is a sampling failure and another draw has a good chance of being valid. Weaker models
    hit this often; gpt-oss-120b emitted '{"name": "concentration", "arguments": {"}"}'."""
    class _E(Exception):
        status_code = 400

    assert is_retryable(_E(_TOOL_JSON))
    assert not is_retryable(_E("invalid api key"))      # a real 400 is still fatal


def test_a_per_day_quota_fails_fast_instead_of_burning_retries():
    """A per-minute limit clears in seconds and is worth waiting for. A per-day cap will
    not clear inside this run, and the first live suite spent six retries discovering
    that. Fail now, with the provider's message, which says when it resets."""
    from xray_agent.adapters.retrying_llm import is_daily_quota

    class _E(Exception):
        status_code = 429

    assert is_daily_quota(_E(_TPD))
    assert not is_daily_quota(_E("Rate limit reached: 30 requests per minute"))

    slept = []

    class Capped:
        model = "fake"

        def generate(self, **kw):
            raise _E(_TPD)

    with pytest.raises(_E):
        RetryingLLM(Capped(), max_retries=6, sleep=slept.append).generate(
            system="s", messages=[])
    assert slept == []          # no retries burned on a quota that cannot clear


def test_retry_after_is_parsed_from_the_message_body_not_just_headers():
    """Groq puts the wait in the body text, not a Retry-After header."""
    class _E(Exception):
        status_code = 429

    assert retry_after_seconds(_E(_TPD)) == pytest.approx(505.872)   # 8m25.872s


def test_an_ordinary_per_minute_limit_is_still_retried():
    slept, calls = [], {"n": 0}

    class _E(Exception):
        status_code = 429

    class Limited:
        model = "fake"

        def generate(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _E("Rate limit reached: 30 requests per minute, try again in 2.0s")
            return _groq_says("ok")

    RetryingLLM(Limited(), sleep=slept.append).generate(system="s", messages=[])
    assert slept == [2.0]
