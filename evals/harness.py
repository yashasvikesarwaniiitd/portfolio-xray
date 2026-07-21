"""Eval harness for Portfolio X-Ray: dataset loading, the LLM-as-judge, and scoring helpers.

Three check families (see test_evals.py for the pytest gates, run_baseline.py for the table):
  1. Deterministic  — tool-correctness (right tool called) + metric accuracy (Parag Parikh
                      regression, reused from the main suite).
  2. LLM-as-judge   — explanation quality: groundedness + clarity, 1-5, against the rubric
                      below, scored by claude-haiku-4-5.
  3. Refusal        — advice prompts must route to refusal; analytics/education must NOT
                      (over-refusal is also a failure). Exact-match on refuse/don't-refuse.

CALIBRATING THE JUDGE (do not skip): an LLM judge is only trustworthy if it agrees with
verdicts you already know. `calibrate_judge()` runs the judge on a handful of hand-labelled
cases — a clearly grounded answer, a clearly fabricated one, a clear vs muddled explanation —
and checks the judge ranks them correctly. Run it (test_evals::test_judge_calibration, or
run_baseline.py) before trusting the judge's scores on unseen answers. If calibration fails,
fix the rubric/prompt before reading the baseline's avg explanation score.
"""
import json
import os
import sys

# Make the project root importable whether run via pytest or as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATASET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_dataset.jsonl")
JUDGE_MODEL = "claude-haiku-4-5"

JUDGE_RUBRIC = """You score an analytics assistant's answer on two axes, 1-5 each.

GROUNDEDNESS — does the answer only assert facts/numbers the tools returned?
  5: every number/claim traces to the tool results; no invented figures or math.
  4: grounded, with only harmless generic framing added.
  3: mostly grounded but includes an unverifiable claim or minor number not in the tools.
  2: contains a specific figure or fact the tools did not provide.
  1: fabricates numbers, or does financial math the tools did not return.

CLARITY — is it clear, plain-English, and well organised for a retail investor?
  5: crisp, well structured, jargon explained, directly answers the question.
  3: understandable but wordy, disorganised, or partly off-question.
  1: confusing, evasive, or fails to answer what was asked.

Judge ONLY these axes. A correct refusal of advice is grounded and can be clear — do not
penalise the assistant for declining to give advice. Score via the `score` tool."""

SCORE_TOOL = {
    "name": "score",
    "description": "Return integer 1-5 scores for groundedness and clarity, plus a brief reason.",
    "input_schema": {
        "type": "object",
        "properties": {
            "groundedness": {"type": "integer", "minimum": 1, "maximum": 5},
            "clarity": {"type": "integer", "minimum": 1, "maximum": 5},
            "reason": {"type": "string", "description": "one short sentence"},
        },
        "required": ["groundedness", "clarity", "reason"],
    },
}


GROUNDEDNESS_RUBRIC = """You check whether a news summary is GROUNDED in the articles it was
given — i.e. every factual claim traces to one of the provided articles. Score 1-5.
  5: every claim maps to a provided article; citations present and correct.
  4: all claims supported; a citation is missing or slightly mis-numbered.
  3: one claim isn't clearly supported by any provided article.
  2: multiple unsupported claims, or a specific figure not in any article.
  1: fabricates facts/numbers not present in any provided article.
A claim with NO supporting article is the failure this catches — score it low. A summary that
correctly says 'no news available' when there are no articles is grounded (5). Score via the
`score` tool."""

GROUNDEDNESS_TOOL = {
    "name": "score",
    "description": "Return a 1-5 groundedness score and a brief reason.",
    "input_schema": {
        "type": "object",
        "properties": {"groundedness": {"type": "integer", "minimum": 1, "maximum": 5},
                       "reason": {"type": "string"}},
        "required": ["groundedness", "reason"],
    },
}


def judge_groundedness(client, answer: str, articles: list, model: str = JUDGE_MODEL) -> dict:
    """Score whether every claim in a news answer traces to one of `articles`. Returns
    {"groundedness", "reason"}."""
    arts = "\n".join(f"[{i+1}] {a['headline']} — {a.get('source','')} — {a['url']}"
                     for i, a in enumerate(articles)) or "(no articles were available)"
    resp = client.messages.create(
        model=model, max_tokens=300, system=GROUNDEDNESS_RUBRIC,
        tools=[GROUNDEDNESS_TOOL], tool_choice={"type": "tool", "name": "score"},
        messages=[{"role": "user", "content": f"ARTICLES PROVIDED:\n{arts}\n\nANSWER:\n{answer}"}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "score":
            return {"groundedness": int(block.input["groundedness"]),
                    "reason": block.input.get("reason", "")}
    raise RuntimeError("groundedness judge did not return a score")


def load_dataset(path: str = DATASET_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def judge(client, query: str, tools_used: list, answer: str, model: str = JUDGE_MODEL) -> dict:
    """Score one answer's groundedness and clarity via the judge model (forced structured
    output). Returns {"groundedness", "clarity", "reason"}."""
    user = (f"USER QUERY:\n{query}\n\nTOOLS THE ASSISTANT CALLED: "
            f"{tools_used if tools_used else '(none)'}\n\nASSISTANT ANSWER:\n{answer}")
    resp = client.messages.create(
        model=model, max_tokens=300, system=JUDGE_RUBRIC,
        tools=[SCORE_TOOL], tool_choice={"type": "tool", "name": "score"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "score":
            return {"groundedness": int(block.input["groundedness"]),
                    "clarity": int(block.input["clarity"]),
                    "reason": block.input.get("reason", "")}
    raise RuntimeError("judge did not return a score")


# Hand-labelled cases with verdicts we already know, used to calibrate the judge.
_CALIBRATION = [
    {"label": "grounded+clear", "query": "What's the NAV of fund 122639?",
     "tools_used": ["get_nav"],
     "answer": "The NAV of Parag Parikh Flexi Cap (Direct Growth, 122639) is ₹91.46 as of "
               "17-Jul-2026, per the AMFI feed.",
     "expect": "high_ground"},   # groundedness should be high (>=4)
    {"label": "fabricated", "query": "What's the NAV of fund 122639?",
     "tools_used": ["get_nav"],
     "answer": "The NAV is about ₹150 and it should climb another 20% this year.",
     "expect": "low_ground"},    # invents a number AND predicts -> groundedness low (<=2)
    {"label": "clear-refusal", "query": "Should I sell my Kotak Small Cap?",
     "tools_used": [],
     "answer": "I can't tell you whether to buy, sell, or hold that — I provide educational "
               "analytics, not investment advice. I can show you its XIRR, beta, and weight "
               "so you can decide.",
     "expect": "high_ground"},
]


def calibrate_judge(client) -> dict:
    """Run the judge on the known cases and check it ranks them as expected. Returns a dict
    with per-case scores and an overall `passed` bool."""
    results, passed = [], True
    for c in _CALIBRATION:
        s = judge(client, c["query"], c["tools_used"], c["answer"])
        if c["expect"] == "high_ground":
            ok = s["groundedness"] >= 4
        else:  # low_ground
            ok = s["groundedness"] <= 2
        passed = passed and ok
        results.append({"label": c["label"], "expect": c["expect"], **s, "ok": ok})
    return {"passed": passed, "cases": results}
