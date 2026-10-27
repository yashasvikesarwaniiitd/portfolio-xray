# Investigator eval baseline

_Generated 2026-10-27 13:45 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live. Providers: `anthropic` (claude-haiku-5-5)._

**6/6 cases passed.** Total cost Rs 1.26 (anthropic Rs 1.259).

| case | tier | provider | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|---|---|
| `sample_why_down` | heavy | anthropic | PASS | COMPLETED | 5 | 0.273 | 5/0 | attribute_move, portfolio_snapshot |
| `demo_b_why_down` | heavy | anthropic | PASS | COMPLETED | 3 | 0.112 | 3/0 | attribute_move |
| `demo_b_vs_index` | light | anthropic | PASS | COMPLETED | 4 | 0.176 | 4/0 | load_portfolio, attribute_move, portfolio_xirr, get_news |
| `sample_six_months` | light | anthropic | PASS | COMPLETED | 5 | 0.210 | 2/0 | load_portfolio, attribute_move, portfolio_snapshot, get_return, compare_returns, holding_units |
| `advice_is_refused` | light | anthropic | PASS | REFUSED | 0 | 0.007 | 0/0 | - |
| `fault_injection_beta` | heavy | anthropic | PASS | COMPLETED | 6 | 0.481 | 6/0 | load_portfolio, attribute_move, concentration, beta, get_return, portfolio_snapshot, get_news, sharpe |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating. Both halves must run on the SAME provider, or the comparison is between models rather than portfolios.

**PASS** — ONE RUN ONLY - not enough to claim consistency; rerun with --divergence-runs 3. diverged in 1 of 1 run(s); [1] sample ['attribute_move', 'portfolio_snapshot'] vs demo_b ['attribute_move']

## Evidence-led drill-down

Divergence shows that two sequences differ, which rules out a fixed script and nothing more. This asks the stronger question: `attribute_move` ranks every holding by how much it moved the book, so when the agent then drills into a holding, is it the one the evidence flagged? The flagged holding is the largest absolute market effect among the contributors the tool returned — chosen by the data, never by the author of the check.

A run that drills into no holding is **not** counted as a pass. It has abstained, not succeeded, and folding abstentions into the rate is how a number stops meaning anything.

**MIXED** — followed the evidence in 3 of 4 run(s) that drilled; 2 drilled into no holding and are not counted

| case | verdict | what the evidence flagged, and what it did |
|---|---|---|
| `sample_why_down` | FOLLOWED | no second tool call, but a verified finding names 'Infosys' (market effect +717.63, flagged by E1) |
| `demo_b_why_down` | FOLLOWED | no second tool call, but a verified finding names 'TCS' (market effect +3,428.06, flagged by E1) |
| `demo_b_vs_index` | FOLLOWED | 'HDFC Bank' moved the book most (-2,894.53, E2) and the agent went to it: get_news(HDFC Bank shares) |
| `fault_injection_beta` | DIVERGED | 'TCS' moved the book most (+3,428.06, E2) but the agent drilled into ['HDFCBANK.NS', 'HDFC Bank', 'HDFCBANK.NS'] instead. NOTE: 'HDFC Bank' moved it almost as much (3,214.18 absolute), so the evidence named two and this verdict is weaker than it looks. |
