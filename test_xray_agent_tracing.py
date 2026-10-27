"""Hermetic tests for the Langfuse tracer, focused on the thing that actually matters:
nothing leaves the machine by default.

No langfuse package needed — the adapter only imports it when it has to build its own
client, so injecting a fake exercises every hook. No keys, no network.
"""
import json
from datetime import date

import pytest

from xray_agent.adapters import tracer_langfuse as tl
from xray_agent.adapters.anthropic_llm import ScriptedLLM, calls, classifies
from xray_agent.adapters.tracer_langfuse import REDACTED, LangfuseTracer
from xray_agent.application.investigator import Investigator
from xray_agent.ports.tools import ToolResult
from test_xray_agent_loop import PRICED_MODEL, WINDOW, FakeTools


class FakeSpan:
    def __init__(self, sink, name, **kw):
        self.sink, self.name, self.kw = sink, name, dict(kw)
        self.scores, self.ended = {}, False
        sink.spans.append(self)

    def start_observation(self, *, name, as_type="span", **kw):
        return FakeSpan(self.sink, name, as_type=as_type, **kw)

    def update(self, **kw):
        self.kw.update(kw)

    def score_trace(self, *, name, value, **kw):
        self.scores[name] = value
        self.sink.scores[name] = value

    def end(self, **kw):
        self.ended = True
        return self


class FakeLangfuse:
    def __init__(self):
        self.spans: list[FakeSpan] = []
        self.scores: dict = {}
        self.flushed = 0

    def start_observation(self, *, name, as_type="span", **kw):
        return FakeSpan(self, name, as_type=as_type, **kw)

    def flush(self):
        self.flushed += 1


POISONED = json.dumps({"contributors": [{"name": "Reliance Industries", "gain": -8100.25}],
                       "change": -12450.5})


def _run(monkeypatch, content_enabled: bool):
    monkeypatch.setenv("XRAY_TRACE_CONTENT", "1" if content_enabled else "0")
    sink = FakeLangfuse()
    tools = FakeTools({"attribute_move": ToolResult(POISONED)})
    script = [
        classifies("analytics"),
        calls(("update_plan", {"steps": ["Decompose the move"]})),
        calls(("attribute_move", WINDOW)),
        calls(("record_finding", {"claim": "Reliance Industries lost 8100.25.",
                                  "evidence_ids": ["E1"]})),
        calls(("record_finding", {"claim": "The book fell 99.9 percent.",
                                  "evidence_ids": ["E1"]})),
        calls(("finish", {"summary": "The portfolio fell 12450.5."})),
    ]
    agent = Investigator(ScriptedLLM(script, model=PRICED_MODEL), tools,
                         tracer=LangfuseTracer(client=sink), today=date(2026, 10, 26),
                         sleep=lambda _s: None)
    state = agent.investigate("Why is my portfolio down?", "portfolio.demo_b.csv")
    return sink, state


def test_no_portfolio_content_is_exported_by_default(monkeypatch):
    """The README promises nothing is stored. A tracer that shipped holdings and amounts
    to a SaaS by default would make that promise false, so the default is a placeholder
    rather than a partial mask — a 'masked' amount is often recoverable from a percentage
    and a total."""
    sink, _ = _run(monkeypatch, content_enabled=False)
    blob = json.dumps([{"name": s.name, "kw": s.kw} for s in sink.spans], default=str)

    assert "Reliance Industries" not in blob      # holding name
    assert "8100.25" not in blob                  # rupee amount
    assert "12450.5" not in blob                  # portfolio move
    assert "Why is my portfolio down?" not in blob
    assert REDACTED in blob


def test_structure_and_metrics_are_still_exported_when_content_is_redacted(monkeypatch):
    """Redaction must not make the trace useless: tool names, evidence IDs, latencies,
    costs, budgets and the rejection CATEGORY all still go out."""
    sink, state = _run(monkeypatch, content_enabled=False)
    names = [s.name for s in sink.spans]

    assert "investigation" in names
    assert "tool.attribute_move" in names
    assert "llm.generate" in names
    assert names.count("verify.finding") == 2
    tool_span = next(s for s in sink.spans if s.name == "tool.attribute_move")
    assert tool_span.kw["metadata"]["evidence_id"] == "E1"
    assert tool_span.kw["metadata"]["ok"] is True
    # The rejected finding is filterable by WHY, without exporting the claim.
    kinds = [s.kw["metadata"]["rejection_kind"] for s in sink.spans
             if s.name == "verify.finding"]
    assert "unsourced_number" in kinds
    assert None in kinds                           # the accepted one


def test_content_is_exported_when_explicitly_enabled(monkeypatch):
    sink, _ = _run(monkeypatch, content_enabled=True)
    blob = json.dumps([{"name": s.name, "kw": s.kw} for s in sink.spans], default=str)
    assert "Reliance Industries" in blob
    assert REDACTED not in blob


def test_run_scores_are_emitted_and_the_client_is_flushed(monkeypatch):
    sink, state = _run(monkeypatch, content_enabled=False)
    assert sink.scores["findings_verified"] == 1
    assert sink.scores["findings_rejected"] == 1
    assert sink.scores["verified_ratio"] == 0.5
    assert sink.scores["evidence_count"] == 1
    assert sink.scores["outcome"] == "COMPLETED"
    assert sink.flushed == 1
    assert all(s.ended for s in sink.spans)


def test_a_broken_tracer_never_breaks_the_investigation(monkeypatch):
    """Observability is not allowed to take a run down. Every hook on an exploding client
    must be swallowed and the investigation must still complete."""
    monkeypatch.delenv("XRAY_TRACE_CONTENT", raising=False)

    class Exploding:
        def start_observation(self, **kw):
            raise RuntimeError("langfuse is down")

        def flush(self):
            raise RuntimeError("still down")

    tools = FakeTools({"attribute_move": ToolResult(POISONED)})
    agent = Investigator(
        ScriptedLLM([classifies("analytics"), calls(("attribute_move", WINDOW)),
                     calls(("finish", {"summary": "Done."}))], model=PRICED_MODEL),
        tools, tracer=LangfuseTracer(client=Exploding()), today=date(2026, 10, 26),
        sleep=lambda _s: None)
    state = agent.investigate("Why am I down?", "portfolio.demo_b.csv")

    assert state.outcome.value == "COMPLETED"
    assert state.summary == "Done."


def test_missing_keys_raise_so_the_cli_can_fall_back(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    pytest.importorskip("langfuse", reason="tracing extra not installed")
    with pytest.raises(Exception):
        LangfuseTracer()


def test_content_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("XRAY_TRACE_CONTENT", "1")
    assert tl.content_enabled()
    monkeypatch.setenv("XRAY_TRACE_CONTENT", "0")
    assert not tl.content_enabled()
    monkeypatch.delenv("XRAY_TRACE_CONTENT")
    assert not tl.content_enabled()


def test_the_generation_span_carries_the_exchange_not_just_its_price(monkeypatch):
    """A generation span showing tokens and cost but no input or output tells you what a
    call cost and nothing about what it did. The port was never handed the prompt or the
    completion, so Langfuse rendered "—" in both columns for every llm.generate."""
    sink, _ = _run(monkeypatch, content_enabled=True)
    gens = [s for s in sink.spans if s.name == "llm.generate"]
    assert gens, "no generation spans at all"

    for g in gens:
        assert "input" in g.kw and g.kw["input"] is not None
        assert "output" in g.kw and g.kw["output"] is not None
    # The completion is normalised rather than provider-native: the same two fields on
    # Anthropic and on Groq, which assistant_content would not give us.
    out = gens[0].kw["output"]
    assert set(out) == {"text", "tool_calls", "stop_reason"}
    # The first turn after the gate asks for update_plan, and the span should say so.
    first_calls = [c["name"] for c in out["tool_calls"]]
    assert "update_plan" in first_calls
    # And the prompt is the message list the model actually saw.
    assert isinstance(gens[0].kw["input"], list)
    assert any("Why is my portfolio down?" in str(m) for m in gens[0].kw["input"])


def test_the_exchange_is_redacted_by_default_like_every_other_payload(monkeypatch):
    """Adding the prompt to the trace adds the QUESTION and, through the tool results
    carried in the message history, the portfolio figures. If that rode out by default it
    would break the same promise the tool spans are careful about."""
    sink, _ = _run(monkeypatch, content_enabled=False)
    gens = [s for s in sink.spans if s.name == "llm.generate"]
    assert gens
    for g in gens:
        assert g.kw["input"] == REDACTED
        assert g.kw["output"] == REDACTED
    blob = json.dumps([{"name": s.name, "kw": s.kw} for s in sink.spans], default=str)
    assert "Why is my portfolio down?" not in blob
    assert "Reliance Industries" not in blob
