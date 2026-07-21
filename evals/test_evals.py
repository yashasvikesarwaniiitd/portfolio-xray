"""Pytest gates for the eval harness. These make live Haiku/network calls (like the main
regression test). The full scored baseline table is produced by run_baseline.py.

Gates here:
- refusal correctness: every advice prompt routes to refusal; nothing else does (no over-refusal)
- judge calibration: the LLM judge agrees with known verdicts before we trust its scores
- metric accuracy: the Parag Parikh reconstruction still matches ground truth
- tool correctness (lightweight): fast analytics prompts call the expected tool
"""
import anthropic
import pytest
from dotenv import load_dotenv

import agent
import harness
import router

load_dotenv()


@pytest.fixture(scope="module")
def client():
    return anthropic.Anthropic()


@pytest.fixture(scope="module")
def dataset():
    return harness.load_dataset()


def test_refusal_correctness_no_leaks_no_overrefusal(client, dataset):
    """Advice must route to refusal; analytics/education/news/offtopic must NOT."""
    leaks, over_refusals = [], []
    for case in dataset:
        category = router.classify(client, case["query"])["category"]
        refused = category == "advice"
        if case["must_refuse"] and not refused:
            leaks.append(case["id"])              # advice slipped through
        if not case["must_refuse"] and refused:
            over_refusals.append(case["id"])      # legit query wrongly refused
    assert not leaks, f"advice leaked past the refusal boundary: {leaks}"
    assert not over_refusals, f"legitimate queries over-refused: {over_refusals}"


def test_judge_calibration(client):
    """The judge must rank known-grounded answers high and fabricated answers low."""
    result = harness.calibrate_judge(client)
    assert result["passed"], f"judge miscalibrated: {result['cases']}"


@pytest.mark.regression
def test_metric_accuracy_parag_parikh():
    """Reused ground-truth lock so metric accuracy is part of the eval gate too. Units are the
    drift-free invariant; live current_value tracks today's NAV (checked for consistency)."""
    from test_agent import PP_HOLDING  # the exact 122639 holding
    r = agent.reconstruct_holding(PP_HOLDING)
    assert r["status"] == "priced"
    assert r["units"] == pytest.approx(1164.541, rel=0.001)
    assert r["current_value"] == pytest.approx(r["units"] * r["current_price"], rel=1e-4)


# A few FAST analytics prompts (single-fetch tools) for tool-correctness in the gate; the
# heavy portfolio-wide tools are exercised in run_baseline.py.
_LIGHT_TOOL_CASES = [
    ("What's the NAV of fund code 122639?", "get_nav"),
    ("What's the current price of INFY.NS?", "get_price"),
    ("What's the beta of RELIANCE.NS?", "beta"),
    ("Show me RELIANCE.NS price history for the last month", "price_history"),
]


@pytest.mark.parametrize("query,expected_tool", _LIGHT_TOOL_CASES)
def test_tool_correctness_lightweight(client, query, expected_tool):
    result = agent.answer_query(client, [], query, {}, log=False)
    assert result["category"] == "analytics"
    assert expected_tool in result["tools_used"], \
        f"expected {expected_tool}, got {result['tools_used']}"


import news  # noqa: E402


@pytest.mark.parametrize("query", ["What's the latest news on Reliance?",
                                   "Any recent news on TCS?"])
def test_news_answer_is_grounded(client, query):
    """A synthesised news answer must be grounded in the articles it was given (every claim
    traces to a provided article). This is the citation guarantee made measurable."""
    articles = news.fetch_news(agent._news_query_from_text(query))["articles"]
    if not articles:
        pytest.skip("no articles returned (upstream/rate-limit) — not a groundedness failure")
    answer = agent._synthesize_news(client, query, {"count": len(articles),
                                                    "articles": articles})
    verdict = harness.judge_groundedness(client, answer, articles)
    assert verdict["groundedness"] >= 4, f"ungrounded news answer: {verdict}"
