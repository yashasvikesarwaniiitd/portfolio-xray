# Convenience targets. On Windows, run the underlying commands directly (they're 1-liners).

test:            ## free, hermetic — what CI runs on every push
	uv run pytest test_metrics.py test_agent.py test_report.py test_xray_agent.py test_xray_agent_loop.py test_xray_agent_tracing.py test_xray_agent_groq.py -m "not regression" -q

test-regression: ## free, needs network (yfinance/AMFI)
	uv run pytest test_metrics.py test_agent.py test_report.py test_xray_agent.py test_xray_agent_loop.py test_xray_agent_tracing.py test_xray_agent_groq.py -m regression -q

fixtures:        ## free (market data only, NO api credits): re-record replay fixtures
	uv run python evals/record_fixtures.py

evals-full:      ## SPENDS API CREDITS (~Rs 5-10): live router/judge/insight evals + baseline
	uv run pytest evals/ -q
	uv run python evals/run_baseline.py

evals-agent:     ## SPENDS API CREDITS (~Rs 3-6): Investigator evals over replay fixtures
	uv run python evals/agent_evals.py

evals-agent-free: ## FREE: the same evals on Groq's free tier (rate limited, expect retries)
	uv run python evals/agent_evals.py --provider groq --out evals/agent_baseline_groq.md

demo:            ## SPENDS API CREDITS (~Rs 1): one investigation, offline market data
	uv run python -m xray_agent investigate "Why is my portfolio down this month?" \
		--portfolio portfolio.demo_b.csv --mode replay --today 2026-10-26

deployed:        ## post-deploy smoke vs live URL: XRAY_BASE_URL=https://... make deployed
	uv run pytest tests/test_deployed.py -v
