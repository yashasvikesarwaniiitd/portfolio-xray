"""Hermetic tests for the eval harness's provider routing.

No API keys, no model calls. Routing decides where money gets spent and whether the
headline divergence result means anything, so it is worth testing as logic rather than
discovering through a bill.

The harness is a script, not a package (evals/ has no __init__.py), so it is loaded by
path the same way a test runner would pick it up.
"""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "agent_evals.py")
_spec = importlib.util.spec_from_file_location("agent_evals", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


class _Args:
    def __init__(self, provider="anthropic", premium=None, routes=None):
        self.provider = provider
        self.premium_provider = premium or provider
        self.route_map = routes or {}


def _plan(args):
    return {c["id"]: ev.resolve_provider(c, args) for c in ev.CASES}


# --- tiers and routing ---------------------------------------------------------------------

def test_every_case_declares_a_tier():
    """A case with no tier silently lands on the default provider, which is how a 'cheap'
    run quietly becomes an expensive one."""
    for c in ev.CASES:
        assert c.get("tier") in ("heavy", "light"), c["id"]


def test_mix_puts_the_expensive_thinking_on_anthropic_and_the_rest_free():
    plan = _plan(_Args(provider="groq", premium="anthropic"))
    assert plan == {
        "sample_why_down": "anthropic",       # divergence pair: the central claim
        "demo_b_why_down": "anthropic",
        "fault_injection_beta": "anthropic",  # longest run, hardest recovery
        "demo_b_vs_index": "groq",
        "sample_six_months": "groq",
        "advice_is_refused": "groq",          # one classifier call, no tools
    }


def test_a_single_provider_routes_everything_to_it():
    assert set(_plan(_Args("groq")).values()) == {"groq"}
    assert set(_plan(_Args("anthropic")).values()) == {"anthropic"}


def test_an_explicit_route_beats_the_tier():
    plan = _plan(_Args(provider="groq", premium="groq",
                       routes={"sample_why_down": "anthropic"}))
    assert plan["sample_why_down"] == "anthropic"
    assert plan["demo_b_vs_index"] == "groq"


def test_bad_route_arguments_are_rejected_before_anything_runs():
    with pytest.raises(SystemExit):
        ev._parse_routes(["no_equals_sign"])
    with pytest.raises(SystemExit):
        ev._parse_routes(["sample_why_down=openai"])      # unknown provider
    with pytest.raises(SystemExit):
        ev._parse_routes(["no_such_case=groq"])           # typo in the case id
    assert ev._parse_routes([" sample_why_down = groq "]) == {"sample_why_down": "groq"}


# --- the divergence pair must not be split ----------------------------------------------

def _rows(p_a, p_b, tools_a, tools_b):
    return [{"id": "sample_why_down", "provider": p_a, "tools": tools_a},
            {"id": "demo_b_why_down", "provider": p_b, "tools": tools_b}]


def test_splitting_the_divergence_pair_warns():
    assert ev.enforce_pair_provider(
        {"sample_why_down": "groq", "demo_b_why_down": "anthropic"})
    assert ev.enforce_pair_provider(
        {"sample_why_down": "groq", "demo_b_why_down": "groq"}) == []


def test_a_split_pair_is_not_comparable_rather_than_a_pass():
    """The whole point of the check is that the only thing differing is the PORTFOLIO. Run
    the halves on different models and a difference proves nothing — reporting PASS there
    would be claiming something the run cannot support."""
    verdict, why = ev.divergence_check(_rows("anthropic", "groq", ["a"], ["b"]))
    assert verdict == "N-A"
    assert "different providers" in why


def test_same_provider_different_sequences_passes():
    verdict, _ = ev.divergence_check(_rows("anthropic", "anthropic", ["a"], ["b"]))
    assert verdict == "PASS"


def test_same_provider_identical_sequences_fails():
    verdict, why = ev.divergence_check(
        _rows("groq", "groq", ["attribute_move"], ["attribute_move"]))
    assert verdict == "FAIL"
    assert "identical tool sequences" in why


def test_a_missing_half_is_not_comparable():
    verdict, _ = ev.divergence_check([{"id": "sample_why_down", "provider": "groq",
                                       "tools": ["a"]}])
    assert verdict == "N-A"


# --- per-provider budgets -------------------------------------------------------------------

def test_groq_gets_a_longer_wall_clock_than_anthropic():
    """Groq's free tier is slow and the clock is checked BEFORE each model call, so a tight
    budget there measures the provider rather than the agent."""
    class A(_Args):
        max_steps, max_cost, max_seconds, retries, verbose = 12, 10.0, None, 6, False
        model = premium_model = None

    pool = ev.LLMPool(A())
    assert pool.budget_for("groq").max_seconds > pool.budget_for("anthropic").max_seconds


def test_an_explicit_max_seconds_overrides_both():
    class A(_Args):
        max_steps, max_cost, max_seconds, retries, verbose = 12, 10.0, 55.0, 6, False
        model = premium_model = None

    pool = ev.LLMPool(A())
    assert pool.budget_for("groq").max_seconds == 55.0
    assert pool.budget_for("anthropic").max_seconds == 55.0
