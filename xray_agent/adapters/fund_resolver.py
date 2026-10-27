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


def _verify_one(name: str, txns: list, master: dict, nav_fn, detail_fn, pool=None) -> dict:
    """Verify `name` against `pool` - [(code, scheme_name)] - or, if none is given, against the
    candidates found in `master`. A one-entry pool is how a REMEMBERED match is re-checked."""
    parts = split_scheme_name(name)
    if pool is None:
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


_MATCH_MEMORY: dict = {}      # name_key -> {"code", "scheme_name"}, for this process


def match_key(name: str) -> str:
    """What identifies a fund NAME for remembering its match. The statement's full name,
    compacted - plan and option words included, because Direct and Regular are different
    schemes with different codes."""
    return compact(name)


class DbMatchStore:
    """Remembered matches in the database (see storage.fund_matches_*). A failure to reach it
    returns "nothing remembered", which costs only the slower path this exists to avoid."""

    def get(self, keys):
        import storage
        return storage.fund_matches_get(list(keys))

    def put(self, items):
        import storage
        return storage.fund_matches_put(list(items))


def _default_store():
    try:
        import storage
        return DbMatchStore() if storage.configured() else None
    except Exception:
        return None


def resolve_holdings(holdings: list, search_fn=None, master_fn=None, nav_fn=None,
                     detail_fn=None, match_store=None) -> list:
    """Fill in symbol/source for holdings that have no price source, in place.

    Each fund is matched to an AMFI scheme AND verified against the NAVs its own statement
    recorded; a fund that cannot be verified is left unresolved (and so reported as
    unpriced) rather than priced against the wrong scheme. Passing `search_fn` selects the
    legacy name-only behaviour, which exists for callers with no NAV data.

    A verified match is REMEMBERED (in this process, and in the database when one is
    configured), so a later request re-checks only that one scheme against the statement
    instead of searching and verifying every candidate. It is still verified every time - a
    remembered match that no longer fits the NAVs falls back to the full search - so the
    memory can only make this faster, never make a wrong match stick. When every fund has a
    remembered match the 14,000-scheme fund list is not even loaded.

    Returns a note per holding describing what happened - resolution is a judgement made on
    the user's behalf, so it should be visible rather than silent.
    """
    if search_fn is not None:
        return _resolve_by_name(holdings, search_fn)
    import agent
    # The database memory is for the real services only. A caller that injects its own master
    # list (a test) must never reach a real database by accident.
    production = master_fn is None and match_store is None
    # The in-process memory follows the same rule: on for the real services, and for a caller
    # that supplies a store on purpose, but off for one that only injects a fund list - or one
    # test's remembered match would leak into the next test's.
    use_memory = production or match_store is not None
    master_fn = master_fn or agent.scheme_master
    injected_nav_fn = nav_fn
    detail_fn = detail_fn or agent.scheme_full_name
    if match_store is None and production:
        match_store = _default_store()
    todo = [h for h in holdings if not h.get("source") and h.get("name")]
    if not todo:
        return []

    def nav_fn_for(h):
        """How to fetch a candidate's NAVs for THIS holding: only the window its purchases
        span. (See agent.nav_window for why a full history per candidate was the cost.) An
        injected function, as tests pass, is used as given."""
        if injected_nav_fn is not None:
            return injected_nav_fn
        from datetime import timedelta
        dates = [t.date for t in (h.get("transactions") or [])]
        lo, hi = min(dates) - timedelta(days=5), max(dates) + timedelta(days=5)
        return lambda code: agent.nav_window(code, lo, hi)

    todo = [h for h in todo if h.get("transactions") or injected_nav_fn is not None]
    # One fund can be held in several holding rows; match each NAME once.
    by_name: dict = {}
    for h in todo:
        by_name.setdefault(h["name"].strip().lower(), h)
    keys = {n: match_key(h["name"]) for n, h in by_name.items()}

    def prefetch(jobs):
        """Every scheme's NAVs are a network call: warm them in parallel before any
        verification reads them."""
        if len(jobs) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(lambda j: _safe(j[0], j[1]), jobs))

    # 1. What is already known: this process first, then the database.
    remembered = ({k: _MATCH_MEMORY[k] for k in keys.values() if k in _MATCH_MEMORY}
                  if use_memory else {})
    unknown = [k for k in keys.values() if k not in remembered]
    if match_store is not None and unknown:
        try:
            found = match_store.get(unknown) or {}
        except Exception:
            found = {}
        if use_memory:
            _MATCH_MEMORY.update(found)
        remembered.update(found)

    # 2. A remembered match is re-verified against its ONE scheme.
    results: dict = {}
    hinted = {n: remembered[k] for n, k in keys.items() if k in remembered}
    prefetch([(nav_fn_for(by_name[n]), hint["code"]) for n, hint in hinted.items()])
    needs_search = [n for n in by_name if n not in hinted]
    for n, hint in hinted.items():
        h = by_name[n]
        r = _verify_one(h["name"], h.get("transactions") or [], {}, nav_fn_for(h), detail_fn,
                        pool=[(hint["code"], hint["scheme_name"])])
        if r.get("error"):
            _MATCH_MEMORY.pop(keys[n], None)      # no longer fits: forget it, search afresh
            needs_search.append(n)
        else:
            results[n] = r

    # 3. Everything else gets the full search. The fund list is loaded only now, and only if
    #    something actually needs it.
    if needs_search:
        try:
            master = master_fn()
        except Exception as e:
            for n in needs_search:
                results[n] = {"error": f"AMFI lookup failed ({e})"}
            needs_search = []
    if needs_search:
        prefetch([(nav_fn_for(by_name[n]), code) for n in needs_search
                  for code, _ in candidate_pool(by_name[n]["name"], master)])
        for n in needs_search:
            h = by_name[n]
            results[n] = _verify_one(h["name"], h.get("transactions") or [], master,
                                     nav_fn_for(h), detail_fn)

    # 4. Remember what was newly verified. Written in the background: it is a head start for
    #    the NEXT request and must not delay this one by a database round trip.
    fresh = [(keys[n], results[n]["fund_code"], results[n]["scheme_name"])
             for n in needs_search if n in results and not results[n].get("error")]
    if use_memory:
        for k, code, name in fresh:
            _MATCH_MEMORY[k] = {"code": str(code), "scheme_name": name}
    if match_store is not None and fresh:
        import threading
        threading.Thread(target=lambda: _safe(match_store.put, fresh), daemon=True).start()

    notes: list = []
    for h in todo:
        key = h["name"].strip().lower()
        r = results[key]
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
