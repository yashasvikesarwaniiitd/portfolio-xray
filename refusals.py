"""Refusal Spec v1 — implemented, not just documented.

The router sends every `advice` query here. We classify the advice sub-type (direct /
disguised / prediction) with cheap keyword heuristics (no API call needed) and return a
fixed refusal that: (a) clearly declines, (b) states the analytics-vs-advice line (educational
analytics only, not SEBI-registered investment advice), (c) redirects to what the agent CAN do.

Refusals are canned strings, NOT model-generated, so the boundary cannot drift or be
talked around by rephrasing. `refusal_message(..., repeat=n)` hardens the wording when the
user keeps re-asking, so we hold the line instead of eventually complying.
"""

SUBTYPES = ["direct", "disguised", "prediction"]

# Stated once per refusal; the SEBI Investment Adviser boundary is the core of the spec.
_SEBI_LINE = ("I provide educational analytics, not investment advice — I'm not a "
              "SEBI-registered investment adviser, so I can't tell you what to buy, sell, "
              "or hold, or where prices are headed.")

_REDIRECT = ("What I can do is lay out the numbers so you can decide for yourself: a "
             "holding's XIRR, beta versus the NIFTY 50, its weight in your portfolio, your "
             "overall concentration and Sharpe ratio, or how two instruments have compared.")

_OPENINGS = {
    "direct": "I can't tell you whether to buy, sell, or hold that.",
    "disguised": "I can't step into an adviser's shoes or tell you what I would do with your "
                 "holdings — that's a recommendation, however it's phrased.",
    "prediction": "I can't forecast where a price, fund, or the market is headed — nobody can "
                  "do that reliably, and a guess dressed up as analysis would do you a "
                  "disservice.",
}

# Keyword cues. Order matters: disguised is checked before prediction before direct so a
# "what would you do" beats an incidental "will".
_DISGUISED_CUES = [
    "if you were", "were my", "your adviser", "your advisor", "as my adviser",
    "as my advisor", "what would you do", "what would you", "would you buy", "would you sell",
    "would you drop", "would you hold", "would you keep", "would you pick", "in your opinion",
    "your opinion", "your pick", "your call", "recommend", "suggestion", "suggest i", "advise me",
]
_PREDICTION_CUES = [
    "will ", "won't", "wont ", "going to", "gonna", "recover", "rebound", "bounce back",
    "good time", "right time", "bad time", "outlook", "forecast", "predict", "prediction",
    "next month", "next year", "next week", "target price", "price target", "reach ", "cross ",
    "hit ", "future of", "expected to",
]


def classify_advice_subtype(query: str) -> str:
    """Best-effort sub-type for tailoring the refusal copy. Defaults to 'direct'."""
    q = f" {query.lower()} "
    if any(cue in q for cue in _DISGUISED_CUES):
        return "disguised"
    if any(cue in q for cue in _PREDICTION_CUES):
        return "prediction"
    return "direct"


def refusal_message(subtype: str, repeat: int = 0) -> str:
    """Compose the refusal. `repeat` is how many times in a row the user has already been
    refused this turn-streak; >=1 adds a firm hold-the-line clause."""
    opening = _OPENINGS.get(subtype, _OPENINGS["direct"])
    parts = [opening, _SEBI_LINE, _REDIRECT]
    if repeat >= 1:
        parts.append("I know you're looking for a steer, and I'm not going to budge on this "
                     "just because it's asked a different way — the analytics are yours to "
                     "act on, but the decision has to be yours.")
    return " ".join(parts)


def refuse(query: str, repeat: int = 0) -> dict:
    """Convenience: classify and compose in one call. Returns {"subtype", "message"}."""
    subtype = classify_advice_subtype(query)
    return {"subtype": subtype, "message": refusal_message(subtype, repeat=repeat)}
