"""Run the full eval harness against the golden dataset and write a scored baseline.

Produces evals/baseline.md and prints the table. Makes many live Haiku + network calls;
expect a few minutes (portfolio-wide tools reconstruct all holdings). Usage:
    uv run python evals/run_baseline.py
"""
import os
import statistics
import sys

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent          # noqa: E402
import harness        # noqa: E402
import news           # noqa: E402
import router         # noqa: E402
from test_agent import PP_HOLDING  # noqa: E402

BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.md")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console: allow ₹, ✅
    load_dotenv()
    client = anthropic.Anthropic()
    dataset = harness.load_dataset()

    rows = []
    for case in dataset:
        route = router.classify(client, case["query"])
        category = route["category"]
        refused = category == "advice"
        rec = {"id": case["id"], "expected_category": case["category"],
               "category": category, "must_refuse": case["must_refuse"], "refused": refused,
               "expected_tools": case.get("expected_tools", []), "tools_used": [],
               "groundedness": None, "clarity": None, "news_groundedness": None}
        # Run the agent (and judge) only where an answer is meaningful to score. A transient
        # API error on one case shouldn't discard the whole multi-minute run — record it and
        # continue so the baseline still writes (the case is flagged, not silently dropped).
        try:
            if category in ("analytics", "education"):
                res = agent.answer_query(client, [], case["query"], {}, log=False)
                rec["category"] = res["category"]
                rec["refused"] = res["category"] == "advice"
                rec["tools_used"] = res["tools_used"]
                score = harness.judge(client, case["query"], res["tools_used"], res["answer"])
                rec["groundedness"], rec["clarity"] = score["groundedness"], score["clarity"]
            elif category == "news" and case.get("groundedness_check"):
                articles = news.fetch_news(
                    agent._news_query_from_text(case["query"]))["articles"]
                answer = agent._synthesize_news(client, case["query"],
                                                {"count": len(articles), "articles": articles})
                rec["news_groundedness"] = harness.judge_groundedness(
                    client, answer, articles)["groundedness"]
        except Exception as e:
            rec["error"] = str(e)[:120]
        rows.append(rec)
        print(f"  [{rec['id']}] route={rec['category']} tools={rec['tools_used']}"
              + (f" ERROR={rec['error']}" if rec.get("error") else ""))

    # --- metrics (exclude cases that errored transiently from scored denominators) ---
    ok_rows = [r for r in rows if not r.get("error")]
    errored = [r["id"] for r in rows if r.get("error")]

    refusal_ok = sum(1 for r in rows if r["refused"] == r["must_refuse"])
    refusal_pct = 100.0 * refusal_ok / len(rows)  # routing decision is made even on error

    router_ok = sum(1 for r in rows if r["category"] == r["expected_category"])
    router_pct = 100.0 * router_ok / len(rows)

    tool_cases = [r for r in ok_rows if r["expected_tools"]]
    tool_ok = sum(1 for r in tool_cases
                  if set(r["expected_tools"]).issubset(set(r["tools_used"])))
    tool_pct = 100.0 * tool_ok / len(tool_cases) if tool_cases else 0.0

    judged = [r for r in rows if r["groundedness"] is not None]
    avg_ground = statistics.mean(r["groundedness"] for r in judged) if judged else 0.0
    avg_clarity = statistics.mean(r["clarity"] for r in judged) if judged else 0.0

    news_judged = [r for r in rows if r["news_groundedness"] is not None]
    avg_news_ground = (statistics.mean(r["news_groundedness"] for r in news_judged)
                       if news_judged else 0.0)

    calib = harness.calibrate_judge(client)

    pp = agent.reconstruct_holding(PP_HOLDING)
    metric_pass = (pp["status"] == "priced"
                   and abs(pp["units"] - 1164.541) / 1164.541 < 0.001
                   and abs(pp["current_value"] - pp["units"] * pp["current_price"]) < 1.0)

    # --- write baseline.md ---
    adv = [r for r in rows if r["must_refuse"]]
    lines = [
        "# Eval baseline — Portfolio X-Ray v0.4",
        "",
        f"Dataset: `golden_dataset.jsonl` ({len(rows)} cases). Router + judge: "
        "`claude-haiku-4-5`. Regenerate with `uv run python evals/run_baseline.py`.",
        "",
        "| Metric | Score |",
        "| --- | --- |",
        f"| Refusal correctness | **{refusal_pct:.0f}%** ({refusal_ok}/{len(rows)}) "
        f"over all cases, incl. {len(adv)} adversarial advice prompts |",
        f"| Router category accuracy | {router_pct:.0f}% ({router_ok}/{len(rows)}) |",
        f"| Tool correctness | {tool_pct:.0f}% ({tool_ok}/{len(tool_cases)}) on analytics cases |",
        f"| Explanation groundedness (1-5) | {avg_ground:.2f} avg over {len(judged)} answers |",
        f"| Explanation clarity (1-5) | {avg_clarity:.2f} avg over {len(judged)} answers |",
        f"| News groundedness (1-5) | {avg_news_ground:.2f} avg over {len(news_judged)} "
        f"cited news answers |",
        f"| Metric accuracy (Parag Parikh regression) | {'PASS' if metric_pass else 'FAIL'} "
        f"(value ₹{pp.get('current_value')}, XIRR {pp.get('xirr_pct')}%) |",
        f"| Judge calibration | {'PASS' if calib['passed'] else 'FAIL'} "
        f"({sum(c['ok'] for c in calib['cases'])}/{len(calib['cases'])} known verdicts agreed) |",
        "",
        (f"> Note: {len(errored)} case(s) errored transiently and were excluded from scored "
         f"denominators: {', '.join(errored)}." if errored else
         "> All cases scored (no transient errors)."),
        "",]
    lines += [
        "## Refusal detail (adversarial prompts)",
        "",
        "| id | query | refused? |",
        "| --- | --- | --- |",
    ]
    for r in adv:
        lines.append(f"| {r['id']} | {next(c['query'] for c in dataset if c['id']==r['id'])} "
                     f"| {'✅' if r['refused'] else '❌ LEAK'} |")
    lines += ["", "## Judge calibration cases", "",
              "| case | expected | groundedness | clarity | ok |",
              "| --- | --- | --- | --- | --- |"]
    for c in calib["cases"]:
        lines.append(f"| {c['label']} | {c['expect']} | {c['groundedness']} | {c['clarity']} "
                     f"| {'✅' if c['ok'] else '❌'} |")
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines[3:14]))
    print(f"\nWrote {BASELINE_PATH}")


if __name__ == "__main__":
    main()
