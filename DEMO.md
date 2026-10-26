# Demo script — the Investigator

Ten minutes, offline-safe, with the exact commands. Full design rationale is in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Before you start

```bash
uv sync
```

`.env` needs `ANTHROPIC_API_KEY`. **Every mode needs it** — replay freezes the market data,
not the model's thinking.

Run this once to prove the machinery works without spending anything:

```bash
uv run pytest test_metrics.py test_agent.py test_report.py test_xray_agent.py test_xray_agent_loop.py -m "not regression" -q
```

Expect **96 passed** in under 10 seconds, with no network and no API key. That is the first
thing worth saying out loud: the whole agent loop — budgets, retries, verification, prompt
injection — is tested as deterministic logic.

---

## The demo

### 1. The problem, in one number (30s, no commands)

> "A SIP portfolio makes 'am I up or down' genuinely hard. Last quarter this demo portfolio's
> value went **up ₹34,353**. The investments actually **lost ₹7,647**. ₹42,000 of new money
> was hiding the loss. Every app I've used would show you the first number."

### 2. Run an investigation (2 min)

```bash
uv run python -m xray_agent investigate "Why is my portfolio down this month?" --portfolio portfolio.demo_b.csv --mode replay --today 2026-10-26
```

`--mode replay` serves market data from recorded fixtures, so **this cannot fail on bad
wifi**. The model's decisions are fully live.

Narrate the trace as it prints:

- **`[1] PLAN`** — the model states how it intends to find out. Not a fixed pipeline.
- **`[2] TOOL attribute_move(...)`** → **`E1`** — every result gets an evidence ID.
- **`FINDING [ACCEPTED] cites E1`** — the claim's numbers were checked against E1 in code.
- **`FINDING [REJECTED] … why: these numbers are not in the cited evidence: 73`** — if one
  appears, stop and point at it. *That is the model being caught inventing a figure, and
  being told exactly which one to drop.*
- **`OUTCOME`** — steps used, rupees spent, verified vs rejected.
- **`COULD NOT DETERMINE`** — the Smallcase basket has no price source, so coverage is below
  100% and the agent says so rather than quietly dropping it.

### 3. Prove it is an agent, not a pipeline (2 min)

Same question, different portfolio:

```bash
uv run python -m xray_agent investigate "Why is my portfolio down this month?" --portfolio portfolio.sample.csv --mode replay --today 2026-10-26
```

> "Different evidence, different investigation. `demo_b` is 46% in one bank, so it chases
> that holding. The sample is spread across six, so it goes to the index comparison instead.
> The eval asserts these sequences differ — if they ever match, the agent is following a
> script and I want the build to fail."

### 4. Show the boundary holding (1 min)

```bash
uv run python -m xray_agent investigate "Should I sell HDFC Bank?" --portfolio portfolio.demo_b.csv --mode replay --today 2026-10-26
```

Outcome is **`REFUSED`**, evidence ledger is **empty**, and no tool ran. The refusal text is
canned, not generated, so it cannot be talked around by rephrasing.

> "The gate is a separate cheap model call rather than a line in the system prompt, because
> it has to fail *closed*. If the classifier errors, it returns `advice` and we refuse. The
> worst case is an unnecessary refusal, never an unguarded answer."

### 5. The part most demos skip (2 min)

Open `test_xray_agent_loop.py` and scroll the test names:

```bash
uv run pytest test_xray_agent_loop.py -q --collect-only
```

> "Budget exhaustion, cost exhaustion, a misfiring wrap-up, permanent failure recovery,
> transient retry, a crash mid-loop, finding repair, cross-evidence citation, caching, loop
> detection, prompt injection through a poisoned holding name, the summary rewrite and its
> template fallback. All hermetic. These are the paths a live demo never reaches and the
> ones that actually decide whether you can ship an agent."

Worth showing one specifically:

```bash
uv run pytest test_xray_agent_loop.py::test_poisoned_holding_name_cannot_smuggle_an_imperative_through -v
```

### 6. Show your own numbers (30s)

```bash
uv run python -m xray_agent history --limit 5
```

Every run is persisted to SQLite — outcome, steps, cost in rupees, verified/total findings.

---

## Live mode (only if the wifi is good)

```bash
uv run python -m xray_agent investigate "Why is my portfolio down this month?" --portfolio portfolio.demo_b.csv --mode live --verbose
```

`--verbose` adds per-call token and cost lines. If anything upstream is flaky, drop back to
`--mode replay` — that is what it is for.

## Running the evals (spends ~₹3-6)

```bash
make evals-agent
```

Writes `evals/agent_baseline.md`: per-case pass/fail, steps, cost, tools used, and the
tool-sequence divergence check. Six cases, including a fault-injection case that breaks
`beta` on purpose to show the agent either routes around it or reports the gap — but never
invents a number.

## Re-recording fixtures (free — market data only, no API credits)

```bash
make fixtures
```

Fixtures were recorded at `--today 2026-10-26`. **Always pass the same `--today`** when
replaying, or the model resolves "this month" to a window that was never recorded. A replay
miss prints a clear error; it never silently falls through to the network.

---

## Langfuse (optional)

```bash
uv sync --extra tracing
```

Then put `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and optionally `LANGFUSE_HOST` in
`.env` and add `--trace langfuse`. If the keys or the package are missing it prints one
line and falls back to the console tracer, so this can never break a demo.

**By default no portfolio content is exported** — tool names, evidence IDs, latencies,
costs, budgets and outcomes go out, but not holding names, rupee amounts or the model's
claims. `XRAY_TRACE_CONTENT=1` opts in, for development only. Say this out loud if anyone
asks about the "nothing stored" promise in the README; it is tested, not just intended.

---

## Questions you will get, and the short answers

**"Is this really agentic?"** The next tool depends on the last result, and the eval asserts
two portfolios produce different sequences. It is not autonomous and not multi-agent —
that's L2/L3 on the ladder in ARCHITECTURE.md §8, and I'd rather say so than oversell it.

**"What stops it making numbers up?"** `verify_finding`. Every numeral in a claim must appear
in the evidence that claim cites. It is code, not a prompt.

**"Why no LangGraph?"** The hard parts are budget accounting, evidence IDs, and
transient-vs-permanent failure classification. A graph library gives you none of those, and
I wanted to be able to explain every line.

**"What's broken?"** `agent.py` still has the original tool loop, so the repo has two. The
chat path uses the old one. Unifying them is the next refactor — it's in ARCHITECTURE.md §11.
