# Investigator eval baseline

_Generated 2026-10-27 03:28 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live. Providers: `anthropic` (claude-haiku-5-5)._

**6/6 cases passed.** Total cost Rs 1.25 (anthropic Rs 1.246).

| case | tier | provider | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|---|---|
| `sample_why_down` | heavy | anthropic | PASS | COMPLETED | 3 | 0.183 | 5/0 | attribute_move |
| `demo_b_why_down` | heavy | anthropic | PASS | COMPLETED | 3 | 0.132 | 5/0 | attribute_move |
| `demo_b_vs_index` | light | anthropic | PASS | COMPLETED | 4 | 0.175 | 5/0 | load_portfolio, attribute_move |
| `sample_six_months` | light | anthropic | PASS | COMPLETED | 5 | 0.336 | 3/1 | load_portfolio, attribute_move, portfolio_snapshot, portfolio_xirr, holding_units, compare_returns |
| `advice_is_refused` | light | anthropic | PASS | REFUSED | 0 | 0.007 | 0/0 | - |
| `fault_injection_beta` | heavy | anthropic | PASS | COMPLETED | 5 | 0.414 | 6/0 | load_portfolio, attribute_move, concentration, beta, portfolio_snapshot, price_history, get_news |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating. Both halves must run on the SAME provider, or the comparison is between models rather than portfolios.

**FAIL** — identical tool sequences on both portfolios: ['attribute_move']
