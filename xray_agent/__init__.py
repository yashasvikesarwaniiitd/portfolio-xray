"""The Investigator: a bounded, verified, traced agent over Portfolio X-Ray's tools.

Layered so the dependency rule points inward:
    domain/       pure Python — no I/O, no SDKs, no imports from adapters
    application/  the agent loop and tool registry; depends on domain + ports
    ports/        typing.Protocol interfaces the application talks to
    adapters/     the messy outside world — Anthropic, yfinance, SQLite, Langfuse
    interfaces/   entry points (CLI)
"""
