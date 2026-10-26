"""Query router: classifies each user query into exactly one category so the agent can
short-circuit advice/off-topic queries before any tool runs.

Uses claude-haiku-5-5 (cheap) with low max_tokens and forced structured output. The router
is a gate, not the answerer — advice and offtopic never reach the tool-calling agent.
"""

ROUTER_MODEL = "claude-haiku-5-5"

CATEGORIES = ["analytics", "news", "education", "advice", "offtopic"]

ROUTER_SYSTEM = """You are the query router for Portfolio X-Ray, an Indian portfolio analytics
assistant. Classify the user's LATEST message into EXACTLY ONE category and give a brief reason.
Classify by intent, not by topic keywords. When a query both asks for analytics AND seeks a
recommendation/prediction, classify it as `advice`.

Categories:
- analytics: wants numbers about THIS user's portfolio or a specific instrument — value, P&L,
  XIRR/returns, beta, Sharpe, concentration, NAV, a stock/crypto/index PRICE or LEVEL, price
  history, units held. Market indices (NIFTY 50, Sensex) and crypto (Bitcoin, BTC) are in
  scope — they are market data this assistant reports, never off-topic.
  e.g. "What's my portfolio XIRR?" / "How concentrated am I?" / "Beta of RELIANCE.NS?" /
  "What's the current NIFTY 50 level?" / "Bitcoin price today?"
- news: asks about recent events/updates/news for a holding, an index, or a market — including
  crypto (Bitcoin) and indices (Nifty).
  e.g. "Any news on Reliance?" / "What happened to Infosys this week?" / "Latest on smallcaps?"
  / "Any updates on Bitcoin?" / "News around the Nifty this week?"
- education: a conceptual/definitional question answerable without the user's data or tools.
  e.g. "What is beta?" / "What does XIRR mean?" / "How is the Sharpe ratio calculated?"
- advice: seeks a buy/sell/hold recommendation, a personal course of action, OR a prediction
  about the future. Includes disguised forms ("what would you do?", "if you were my adviser",
  "which holding would you drop?") and predictions ("will X go up?", "is now a good time?").
  e.g. "Should I sell Kotak Small Cap?" / "What would you do with my portfolio?" /
  "Will smallcaps recover?"
- offtopic: unrelated to investing, markets, or this portfolio.
  e.g. "What's the weather?" / "Write a poem." / "Who won the match?" / "Capital of France?"

Always respond by calling the `classify` tool."""

CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Return the single best category for the user's query and a brief reason.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES,
                         "description": "the single best-fitting category"},
            "reason": {"type": "string", "description": "one short sentence, why"},
        },
        "required": ["category", "reason"],
    },
}


def classify(client, query: str, model: str = ROUTER_MODEL, usage=None) -> dict:
    """Return {"category", "reason"}. On any parse/API oddity, default to 'advice' — the
    safe failure mode is to refuse rather than risk routing a hidden advice ask to the tools.
    If a logger.Usage is passed, the classifier call's token usage is recorded on it."""
    try:
        resp = client.messages.create(
            # Classification needs no deep reasoning, and Haiku 5.5's adaptive
            # thinking would otherwise eat a 200-token budget shared with a forced
            # tool call.
            model=model, max_tokens=400, output_config={"effort": "low"},
            system=ROUTER_SYSTEM,
            tools=[CLASSIFY_TOOL], tool_choice={"type": "tool", "name": "classify"},
            messages=[{"role": "user", "content": query}],
        )
        if usage is not None:
            usage.add(model, resp)
        for block in resp.content:
            if block.type == "tool_use" and block.name == "classify":
                cat = block.input.get("category")
                if cat in CATEGORIES:
                    return {"category": cat, "reason": block.input.get("reason", "")}
    except Exception as e:
        # Still refuse - that is the safe direction - but mark it DEGRADED. An outage and
        # a compliance decision must not be indistinguishable to the caller: the deployed
        # app once refused every single query, including "What is beta?", because its API
        # key was missing, and it looked exactly like the product working as designed.
        return {"category": "advice", "degraded": True,
                "reason": f"router error, failing safe: {e}"}
    return {"category": "advice", "degraded": True,
            "reason": "router returned no usable category; failing safe"}
