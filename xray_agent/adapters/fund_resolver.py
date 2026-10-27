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
NOISE = {"plan", "option", "fund", "fof", "scheme", "mutual", "the", "of", "and",
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


def _resolve_by_name(holdings: list, search_fn) -> list:
    """NAME-ONLY resolution. Kept for callers that have no NAV data to check against.

    This is the path that priced "Kotak Small Cap Fund" on "Kotak Nifty SmallCap 250 Index
    Fund" - a different fund with a NAV of ~10 instead of ~300, which turned a roughly flat
    holding into a fabricated 96% loss. A name match is a guess. Prefer resolve_holdings()
    without a search_fn, which verifies against the statement's own NAVs.
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


# --- verification against the statement's own NAVs ---------------------------------------
#
# A statement records, for every purchase, the NAV it was allotted at. That is independent
# evidence of WHICH scheme the holding is, and it is far stronger than any name match: the
# right scheme's NAV on the purchase date equals the statement's NAV to the paisa, and a
# different scheme's does not come close (a measured example: 14 of 14 purchases matched the
# right Kotak Small Cap scheme and 0 of 14 matched the index fund the name match had chosen).
# It also settles Direct vs Regular and Growth vs IDCW, because those carry different NAVs.

NAV_TOLERANCE = 0.0015      # 0.15%: statements round NAV to 2dp, so a true match is ~0.01%
NAV_WINDOW_DAYS = 3         # allotment can follow the order date by a non-business day or two
MIN_MATCH_SHARE = 0.8       # a few stray rows (a corrected NAV) must not fail a right scheme


def compact(text: str) -> str:
    """Lowercase letters and digits only, so "Small Cap" and "SmallCap" are one string."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower().replace("&", "and"))


def nav_match(series: dict, txns: list, tol: float = NAV_TOLERANCE,
              window: int = NAV_WINDOW_DAYS) -> tuple:
    """(matched, total): how many of the statement's purchases sit at this scheme's NAV.

    For each transaction the closest NAV within `window` days is used, because the date on a
    statement is the order date and the NAV applied can be the next business day's.

    A purchase dated BEFORE the scheme's first published NAV is not counted at all: it is
    untestable, not wrong. The real case is a new-fund offer (NFO), allotted at the Rs 10 face
    value some days before the scheme's NAV history begins - counting it as a mismatch failed
    a correctly identified fund. `untestable` is available from nav_untestable().
    """
    from datetime import timedelta
    matched = total = 0
    first = min(series) if series else None
    for t in txns:
        if not getattr(t, "price", 0):
            continue
        if first is None or t.date < first - timedelta(days=window):
            continue
        total += 1
        best = None
        for k in range(-window, window + 1):
            v = series.get(t.date + timedelta(days=k))
            if v:
                err = abs(v - t.price) / t.price
                best = err if best is None else min(best, err)
        if best is not None and best <= tol:
            matched += 1
    return matched, total


def nav_untestable(series: dict, txns: list, window: int = NAV_WINDOW_DAYS) -> int:
    """Purchases that predate the scheme's first NAV (see nav_match)."""
    from datetime import timedelta
    if not series:
        return len([t for t in txns if getattr(t, "price", 0)])
    first = min(series)
    return len([t for t in txns if getattr(t, "price", 0)
                and t.date < first - timedelta(days=window)])


def candidate_pool(name: str, master: dict, max_extra: int = 6) -> list:
    """AMFI codes whose scheme name could be this fund, best name matches first.

    Contiguous containment on the compacted name, not an all-words search: "kotaksmallcap"
    is not inside "kotakniftysmallcap250indexfund", so the index fund is never a candidate,
    where an all-words match put it alongside (and, after sorting, ahead of) the real fund.
    Candidates whose own core name EQUALS the statement's come first; only if there are none
    do looser containing names get a look, nearest first.
    """
    parts = split_scheme_name(name)
    want = compact(parts["core"])
    if not want:
        return []
    pool = [(code, nm) for code, nm in master.items() if want in compact(nm)]
    exact = [(c, n) for c, n in pool if compact(split_scheme_name(n)["core"]) == want]
    if exact:
        return exact
    return sorted(pool, key=lambda cn: len(compact(cn[1])))[:max_extra]


def _verify_one(name: str, txns: list, master: dict, nav_fn, detail_fn) -> dict:
    parts = split_scheme_name(name)
    pool = candidate_pool(name, master)
    if not pool:
        return {"error": f"no AMFI scheme looks like {parts['core']!r}", **parts}
    scored = []
    for code, nm in pool:
        try:
            m, n = nav_match(nav_fn(code), txns)
        except Exception:
            m, n = 0, len(txns)
        scored.append((m, n, code, nm))
    # Each candidate is judged on ITS OWN testable purchases (they differ: a newer scheme has
    # fewer NAVs to test against), ranked by share matched and then by how much evidence
    # there was.
    def share(s):
        return s[0] / s[1] if s[1] else 0.0

    top = max(scored, key=lambda s: (share(s), s[0]))
    if not top[1]:
        return {"error": "the statement has no purchases that can be checked against a "
                         "published NAV", **parts}
    if share(top) < MIN_MATCH_SHARE:
        return {"error": (f"no AMFI scheme matches the NAVs on your statement (the closest, "
                          f"{top[3]}, matched {top[0]} of {top[1]} purchases); not pricing it "
                          "on a guess"), **parts}
    tied = [s for s in scored if (share(s), s[0]) == (share(top), top[0])]
    total = top[1]
    best_m = top[0]
    ambiguous = False
    if len(tied) > 1:
        # Identical NAV histories: typically Growth and IDCW before any distribution, or
        # Direct and Regular on a brand-new fund. Fall back to the plan/option the statement
        # asked for, using the per-scheme full names.
        ranked = sorted(
            tied, key=lambda s: -score_candidate(detail_fn(s[2]) or s[3], parts["plan"],
                                                 parts["options"]))
        top_score = score_candidate(detail_fn(ranked[0][2]) or ranked[0][3], parts["plan"],
                                    parts["options"])
        runners = [s for s in ranked[1:] if score_candidate(
            detail_fn(s[2]) or s[3], parts["plan"], parts["options"]) == top_score]
        ambiguous = bool(runners)
        chosen = ranked[0]
    else:
        chosen = tied[0]
    full = (detail_fn(chosen[2]) or chosen[3]) if len(tied) > 1 else chosen[3]
    return {**parts, "fund_code": chosen[2], "scheme_name": full, "matched": chosen[0],
            "total": total, "ambiguous": ambiguous, "verified": True,
            "untestable": nav_untestable(nav_fn(chosen[2]), txns)}


def resolve_holdings(holdings: list, search_fn=None, master_fn=None, nav_fn=None,
                     detail_fn=None) -> list:
    """Fill in symbol/source for holdings that have no price source, in place.

    Each fund is matched to an AMFI scheme AND verified against the NAVs its own statement
    recorded; a fund that cannot be verified is left unresolved (and so reported as
    unpriced) rather than priced against the wrong scheme. Passing `search_fn` selects the
    legacy name-only behaviour, which exists for callers with no NAV data.

    Returns a note per holding describing what happened - resolution is a judgement made on
    the user's behalf, so it should be visible rather than silent.
    """
    if search_fn is not None:
        return _resolve_by_name(holdings, search_fn)
    import agent
    master_fn = master_fn or agent.scheme_master
    nav_fn = nav_fn or agent.nav_history
    detail_fn = detail_fn or agent.scheme_full_name
    todo = [h for h in holdings if not h.get("source") and h.get("name")]
    if not todo:
        return []
    try:
        master = master_fn()
    except Exception as e:
        return [f"{h['name']}: AMFI lookup failed ({e})" for h in todo]

    # One fund can be held in several holding rows, and every candidate's NAV history is a
    # network call, so warm the histories in parallel before any verification reads them.
    pools = {h["name"].strip().lower(): candidate_pool(h["name"], master) for h in todo}
    codes = sorted({c for pool in pools.values() for c, _ in pool})
    if len(codes) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda c: _safe(nav_fn, c), codes))

    notes: list = []
    cache: dict = {}
    for h in todo:
        key = h["name"].strip().lower()
        if key not in cache:
            cache[key] = _verify_one(h["name"], h.get("transactions") or [], master,
                                     nav_fn, detail_fn)
        r = cache[key]
        if r.get("error"):
            notes.append(f"{h['name']}: {r['error']}")
            continue
        h["symbol"] = str(r["fund_code"])
        h["source"] = "mftool"
        h["type"] = h.get("type") or "Mutual Fund"
        h["market"] = h.get("market") or "India"    # AMFI schemes are Indian by construction
        detail = (f"{h['name']} -> AMFI {r['fund_code']} ({r['scheme_name']})  "
                  f"[verified: {r['matched']} of {r['total']} purchases sit at this scheme's NAV")
        if r.get("untestable"):
            detail += (f"; {r['untestable']} predate its first published NAV, e.g. a "
                       "new-fund offer allotment")
        detail += "]"
        if r.get("ambiguous"):
            detail += ("  [AMBIGUOUS: another variant has identical NAVs; check the plan and "
                       "option are right]")
        notes.append(detail)
    return notes


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception:
        return None
