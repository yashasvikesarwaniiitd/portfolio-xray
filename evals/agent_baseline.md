# Investigator eval baseline

_Generated 2026-10-27 00:57 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live. Providers: `anthropic` (claude-haiku-4-5)._

**3/3 cases passed.** Total cost Rs 16.73 (anthropic Rs 16.732).

| case | tier | provider | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|---|---|
| `sample_why_down` | heavy | anthropic | PASS | COMPLETED | 4 | 2.406 | 5/0 | load_portfolio, portfolio_snapshot, attribute_move |
| `demo_b_why_down` | heavy | anthropic | PASS | COMPLETED | 9 | 8.690 | 5/0 | portfolio_snapshot, attribute_move, load_portfolio, concentration, get_news, portfolio_xirr, holding_units |
| `fault_injection_beta` | heavy | anthropic | PASS | COMPLETED | 9 | 5.636 | 3/1 | load_portfolio, portfolio_snapshot, attribute_move, holding_units, beta, sharpe |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating. Both halves must run on the SAME provider, or the comparison is between models rather than portfolios.

**PASS** — sample ['load_portfolio', 'portfolio_snapshot', 'attribute_move']  vs  demo_b ['portfolio_snapshot', 'attribute_move', 'load_portfolio', 'concentration', 'get_news', 'get_news', 'portfolio_xirr', 'holding_units']
