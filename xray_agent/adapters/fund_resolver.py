"""Resolve a mutual fund SCHEME NAME from a statement to an AMFI scheme code.

A broker's MF statement identifies a fund the way a human does — "Parag Parikh Flexi Cap
Fund - Direct Growth" — while every price lookup needs the AMFI code (122639). Bridging
that is the only thing standing between an MF statement and a valued portfolio.

The naive approach fails, and it is worth knowing why before trusting this. AMFI's
scheme-code master list has stopped carrying the plan/option qualifier for at least some
funds: all four variants of the fund above come back named plainly "Parag Parikh Flexi Cap
Fund". So searching the full statement name all-words finds NOTHING, because "direct" and
"growth" appear in the query and in no master-list name.

So the name is split in two directions:

    core    "parag parikh flexi cap"   -> what we SEARCH on
    plan    direct | regular            -> what we use to PICK among the variants
    option  growth | idcw | ...

Search on the core, then score the candidates (whose full names agent.search_fund recovers
per-scheme) against the plan and option the statement actually stated. Picking Direct
Growth by default would quietly put a user holding the Regular plan on the wrong NAV and
the wrong expense ratio.

The split and the scoring are pure and tested offline. Only `resolve_scheme` touches the
network, through an injected search function, so the whole thing is testable without it.
"""
from __future__ import annotations

import json
import re

PLAN_WORDS = {"direct": "direct", "regular": "regular"}
# IDCW is the current name for what statements still often call dividend.
OPTION_WORDS = {
    "growth": "growth",
    "idcw": "idcw", "dividend": "idcw", "div": "idcw",
    "payout": "payout", "reinvestment": "reinvestment", "reinvest": "reinvestment",
}
# Words that identify no fund and only break an all-words match.
NOISE = {"plan", "option", "fund", "scheme", "mutual", "the", "of", "and",
         "payout", "reinvestment", "reinvest", "monthly", "quarterly", "daily",
         "weekly", "annual", "half", "yearly", "income", "distribution", "cum",
         "capital", "withdrawal"}

_WORD = re.compile(r"[a-z0-9]+")


def split_scheme_name(name: str) -> dict:
    """{core, plan, option, options} from a statement's scheme name.

    `options` keeps every option token seen, because "IDCW Payout" is two of them and the
    distinction between payout and reinvestment is a different scheme code.
    """
    words = _WORD.findall(str(name or "").lower())
    plan = None
    options: list = []
    core: list = []
    for w in words:
        if w in PLAN_WORDS and plan is None:
            plan = PLAN_WORDS[w]
        elif w in OPTION_WORDS:
            mapped = OPTION_WORDS[w]
            if mapped not in options:
                options.append(mapped)
        elif w not in NOISE:
            core.append(w)
    return {"core": " ".join(core), "plan": plan,
            "option": options[0] if options else None, "options": options}


def score_candidate(full_name: str, plan: str | None, options: list) -> int:
    """How well an AMFI scheme name matches the plan/option the statement asked for.

    Deliberately asymmetric: matching the requested plan is worth more than matching the
    option, because Direct vs Regular changes the expense ratio and therefore every return
    number, while Growth vs IDCW changes the NAV series. Contradicting the statement is
    penalised, not merely unrewarded — a Regular-plan holder must never be silently valued
    on the Direct NAV.
    """
    text = str(full_name or "").lower()
    score = 0
    if plan:
        if plan in text:
            score += 10
        elif any(other in text for other in PLAN_WORDS if other != plan):
            score -= 10
    for i, opt in enumerate(options or []):
        if opt in text:
            score += 5 - i
    if not options and "growth" in text:
        score += 1          # a statement that says nothing is most often Growth
    return score


def pick_best(candidates: list, plan: str | None, options: list) -> dict | None:
    """Highest-scoring candidate; ties fall to the first, which search_fund already sorts
    Direct-Growth first."""
    if not candidates:
        return None
    ranked = sorted(candidates,
                    key=lambda c: -score_candidate(c.get("scheme_name", ""), plan, options))
    best = ranked[0]
    best_score = score_candidate(best.get("scheme_name", ""), plan, options)
    runners = [c for c in ranked[1:]
               if score_candidate(c.get("scheme_name", ""), plan, options) == best_score]
    return {"fund_code": best.get("fund_code"),
            "scheme_name": best.get("scheme_name"),
            "score": best_score,
            "ambiguous": bool(runners),
            "considered": len(candidates)}


def resolve_scheme(name: str, search_fn=None) -> dict:
    """Statement scheme name -> {fund_code, scheme_name, ...} or {"error": ...}.

    `search_fn(query) -> json string` defaults to agent.search_fund. Injected so the
    scoring can be tested without touching AMFI.
    """
    parts = split_scheme_name(name)
    if not parts["core"]:
        return {"error": f"no searchable words in scheme name {name!r}", **parts}
    if search_fn is None:
        import agent
        search_fn = agent.search_fund
    try:
        result = json.loads(search_fn(parts["core"]))
    except Exception as e:
        return {"error": f"AMFI lookup failed: {e}", **parts}

    best = pick_best(result.get("matches") or [], parts["plan"], parts["options"])
    if not best or not best.get("fund_code"):
        return {"error": f"no AMFI scheme matched {parts['core']!r}", **parts}
    return {**parts, **best}


def resolve_holdings(holdings: list, search_fn=None) -> list:
    """Fill in symbol/source for holdings that have no price source, in place.

    Returns a note per holding describing what happened — resolution is a guess made on
    the user's behalf, so it should be visible rather than silent.
    """
    notes: list = []
    cache: dict = {}
    for h in holdings:
        if h.get("source") or not h.get("name"):
            continue
        key = h["name"].strip().lower()
        if key not in cache:
            cache[key] = resolve_scheme(h["name"], search_fn=search_fn)
        r = cache[key]
        if r.get("error"):
            notes.append(f"{h['name']}: {r['error']}")
            continue
        h["symbol"] = str(r["fund_code"])
        h["source"] = "mftool"
        h["type"] = h.get("type") or "Mutual Fund"
        h["market"] = h.get("market") or "India"    # AMFI schemes are Indian by construction
        detail = f"{h['name']} -> AMFI {r['fund_code']} ({r['scheme_name']})"
        if r.get("ambiguous"):
            detail += ("  [AMBIGUOUS: several variants scored equally; check the plan and "
                       "option are right]")
        elif r.get("plan") is None:
            detail += "  [statement did not state Direct/Regular; assumed the default]"
        notes.append(detail)
    return notes
