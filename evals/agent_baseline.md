# Investigator eval baseline

_Generated 2026-10-27 13:14 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live. Providers: `anthropic` (claude-haiku-5-5)._

**6/6 cases passed.** Total cost Rs 1.14 (anthropic Rs 1.139).

| case | tier | provider | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|---|---|
| `sample_why_down` | heavy | anthropic | PASS | COMPLETED | 7 | 0.301 | 4/1 | load_portfolio, attribute_move |
| `demo_b_why_down` | heavy | anthropic | PASS | COMPLETED | 4 | 0.133 | 3/0 | load_portfolio, attribute_move |
| `demo_b_vs_index` | light | anthropic | PASS | COMPLETED | 5 | 0.205 | 5/0 | load_portfolio, attribute_move, portfolio_xirr, get_news |
| `sample_six_months` | light | anthropic | PASS | COMPLETED | 5 | 0.203 | 2/0 | attribute_move, load_portfolio, portfolio_snapshot, get_return, compare_returns |
| `advice_is_refused` | light | anthropic | PASS | REFUSED | 0 | 0.007 | 0/0 | - |
| `fault_injection_beta` | heavy | anthropic | PASS | COMPLETED | 5 | 0.290 | 4/1 | load_portfolio, attribute_move, concentration, beta, portfolio_snapshot |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating. Both halves must run on the SAME provider, or the comparison is between models rather than portfolios.

**PASS** — diverged in 3 of 3 run(s); [1] sample ['load_portfolio', 'attribute_move', 'attribute_move'] vs demo_b ['load_portfolio', 'attribute_move']  |  [2] sample ['attribute_move'] vs demo_b ['load_portfolio', 'attribute_move', 'portfolio_snapshot', 'attribute_move', 'holding_units', 'get_news']  |  [3] sample ['attribute_move'] vs demo_b ['load_portfolio', 'attribute_move', 'portfolio_snapshot']
