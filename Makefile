# Convenience targets. On Windows, run the underlying commands directly (they're 1-liners).

test:            ## free, hermetic — what CI runs on every push
	uv run pytest test_metrics.py test_agent.py test_report.py -m "not regression" -q

test-regression: ## free, needs network (yfinance/AMFI)
	uv run pytest test_metrics.py test_agent.py test_report.py -m regression -q

evals-full:      ## SPENDS API CREDITS (~Rs 5-10): live router/judge/insight evals + baseline
	uv run pytest evals/ -q
	uv run python evals/run_baseline.py

deployed:        ## post-deploy smoke vs live URL: XRAY_BASE_URL=https://... make deployed
	uv run pytest tests/test_deployed.py -v
