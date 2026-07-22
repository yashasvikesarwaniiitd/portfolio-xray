"""CLI: uv run python -m report [portfolio_csv] [out.xlsx] [--no-ai]

--no-ai skips the Haiku insight calls (deterministic fallback blocks; zero API cost).
"""
import sys

from report import generate_report


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if a != "--no-ai"]
    use_ai = "--no-ai" not in sys.argv[1:]
    portfolio = args[0] if len(args) > 0 else "portfolio.csv"
    out = args[1] if len(args) > 1 else "Portfolio_Health_Report.xlsx"
    res = generate_report(portfolio, out, use_ai=use_ai)
    print(f"Report written: {res['out_path']}")
    print(f"  input level {res['level']} | {res['sheets']} sheets | "
          f"locked: {res['locked_sections'] or 'none'} | AI: {res['ai_mode']}")
    c = res["insight_cost"]
    print(f"  insight cost: Rs {c['est_cost_inr']} (${c['est_cost_usd']}, "
          f"{c['input_tokens']}+{c['output_tokens']} tokens) | {res['elapsed_s']}s")


if __name__ == "__main__":
    main()
