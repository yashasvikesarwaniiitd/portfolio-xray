# Investigator eval baseline

_Generated 2026-10-27 00:43 by `uv run python evals/agent_evals.py`. Market data from recorded fixtures (frozen at 2026-10-26); model decisions live on `groq/openai/gpt-oss-120b`._

**1/3 cases passed.** Total cost Rs 0.00.

| case | pass | outcome | steps | Rs | verified/rejected | tools used |
|---|---|---|---|---|---|---|
| `demo_b_why_down` | FAIL | FAILED | 3 | 0.000 | 0/0 | attribute_move, get_news |
| `advice_is_refused` | PASS | REFUSED | 0 | 0.000 | 0/0 | - |
| `fault_injection_beta` | FAIL | FAILED | 2 | 0.000 | 0/0 | attribute_move |

## Tool-sequence divergence

The same question over two different portfolios must produce different tool sequences — identical ones would mean the model is following a script, not investigating.

**FAIL** — both why_down cases must run

## Failures

- `demo_b_why_down`: outcome RunOutcome.FAILED not in ['COMPLETED']
- `demo_b_why_down`: unpriceable holding was never mentioned
- `fault_injection_beta`: outcome RunOutcome.FAILED not in ['COMPLETED', 'INCOMPLETE_BUDGET']
