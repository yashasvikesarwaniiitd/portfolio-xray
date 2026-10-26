# Investigator eval baseline

_Generated 2026-10-27 02:31 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live. Providers: `anthropic` (claude-haiku-5-5)._

**3/3 cases passed.** Total cost Rs 0.61 (anthropic Rs 0.610).

| case | tier | provider | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|---|---|
| `sample_why_down` | heavy | anthropic | PASS | COMPLETED | 3 | 0.111 | 3/0 | attribute_move |
| `demo_b_why_down` | heavy | anthropic | PASS | COMPLETED | 5 | 0.251 | 3/0 | load_portfolio, attribute_move, portfolio_snapshot, get_news |
| `fault_injection_beta` | heavy | anthropic | PASS | COMPLETED | 5 | 0.247 | 4/0 | load_portfolio, attribute_move, concentration, beta, price_history |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating. Both halves must run on the SAME provider, or the comparison is between models rather than portfolios.

**PASS** — sample ['attribute_move']  vs  demo_b ['load_portfolio', 'attribute_move', 'portfolio_snapshot', 'get_news']
