"""Full disclosed holdings for Indian mutual funds, from a frozen monthly snapshot.

WHY THIS EXISTS. Until now `report.data.section_overlap` could only see what yfinance
discloses: a fund's top TEN holdings, matched on name strings. For an Indian mutual fund
it saw nothing at all — those rows arrive through AMFI with no yfinance symbol, so the
filter at the top of that function skipped every one of them. A book made entirely of
Indian funds got an empty overlap section and a diversification score computed with no
overlap penalty at all. This module is what fills that in.

WHY A SNAPSHOT AND NOT A FETCH. SEBI requires every AMC to publish a full monthly
portfolio in a standard template, but each one publishes it on its own site in its own
layout, and some block automated downloads outright. Scraping that inside a user's request
would put a brittle, slow, failure-prone network call on the critical path to answering a
question about overlap. So the fetch happens once, out of band, and the result is
committed as data/fund_holdings_snapshot.json with an as-of date and a source URL per
fund. Nothing here touches the network, which also means every test is hermetic. The cost
is staleness: month-end portfolios publish about ten days late, so a snapshot is between
three and ten weeks old. That is why `as_of` is carried through to the caller rather than
hidden — an overlap number without its date is a claim about today that is not true.

WHAT COUNTS AS OVERLAP. Only instruments that are a claim on a company: equity, foreign
equity, and REITs/InvITs. TREPS, repo, net receivables, t-bills, CDs and CPs are excluded,
because two funds both parking cash overnight is not duplicated exposure. Units of another
fund are excluded too — that is a look-through problem, not an overlap, and it is exactly
what makes the Axis FoF un-seeable.

THE VALIDATION GATE. A fund is accepted only if it passes `problems_with`. A half-read
sheet silently becomes a fund with ten holdings and a plausible-looking overlap number,
which is worse than no answer, so a fund that fails is downgraded to UNAVAILABLE carrying
the reason. This mirrors xray_agent's verify_finding: the parse is not trusted merely
because a parser produced it.
"""
from __future__ import annotations

import json
import os
import re

from xray_agent.adapters.fund_resolver import split_scheme_name

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "fund_holdings_snapshot.json")

# A claim on a company. See the module docstring for why cash, debt and fund units are not.
EQUITY_KINDS = ("EQUITY", "FOREIGN_EQUITY", "REIT_INVIT")

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# The disclosed weights should sum to ~100. The tolerance is wide enough for one real
# case: a fund running arbitrage lists its short futures BELOW the grand total, so the
# rows sum to 100 above the line and to less than that once the hedges are included.
WEIGHT_SUM_TOLERANCE = 3.0

_cache: dict = {}


def problems_with(fund: dict) -> str | None:
    """The gate. Returns a reason to reject, or None to accept.

    This checks what a broken parse breaks, not what the schema already guarantees: a
    truncated sheet shows up as weights that no longer sum to 100, a column read from the
    wrong place shows up as malformed ISINs, and a sheet read twice shows up as a repeated
    ISIN.
    """
    hold = fund.get("holdings") or []
    if not hold:
        return "no holdings in the snapshot"
    seen = set()
    for h in hold:
        isin = h.get("isin")
        if isin is None:
            continue            # legitimate: TREPS, repo, cash, listed derivatives
        if not ISIN_RE.match(str(isin)):
            return "malformed ISIN %r" % (isin,)
        if isin in seen:
            return "ISIN %s appears twice" % isin
        seen.add(isin)
    total = sum(float(h.get("weight_pct") or 0.0) for h in hold)
    if abs(total - 100.0) > WEIGHT_SUM_TOLERANCE:
        return ("disclosed weights sum to %.2f%%, not ~100%% — the sheet looks partial"
                % total)
    if not fund.get("as_of"):
        return "no as-of date"
    if not fund.get("source_url"):
        return "no source URL"
    return None


def _normalise(doc: dict) -> dict:
    """Apply the gate to every fund, downgrading the ones that fail."""
    out = []
    for f in doc.get("funds") or []:
        f = dict(f)
        if f.get("status") == "OK":
            why = problems_with(f)
            if why:
                f["status"] = "UNAVAILABLE"
                f["unavailable_reason"] = "rejected by validation: " + why
                f["holdings"] = []
        out.append(f)
    doc = dict(doc)
    doc["funds"] = out
    return doc


def load(path: str | None = None) -> dict:
    """The snapshot, validated and cached. Never raises: an unreadable or unparseable file
    leaves the app exactly as it was before this module existed."""
    path = path or SNAPSHOT_PATH
    if path in _cache:
        return _cache[path]
    try:
        with open(path, encoding="utf-8") as fh:
            doc = _normalise(json.load(fh))
    except Exception:
        doc = {"schema_version": 0, "generated_at": None, "funds": []}
    _cache[path] = doc
    return doc


def _match_key(name: str) -> str:
    """The core of a scheme name with the spaces removed.

    split_scheme_name already strips the plan, the option and the noise words, leaving
    "parag parikh flexi cap". De-spacing on top of that is what makes "Kotak Small Cap"
    and "Kotak Smallcap Fund" — the same fund, tokenised differently by two sources — land
    on one key.
    """
    return (split_scheme_name(name or "")["core"] or "").replace(" ", "")


def match(name: str, doc: dict | None = None) -> dict | None:
    """Find the snapshot fund a portfolio row names, or None.

    The plan is deliberately ignored. A monthly portfolio is disclosed per SCHEME, and
    Direct and Regular are the same portfolio at different expense ratios — so a holder of
    the Regular plan gets the right holdings here, and refusing to match would be wrong.
    """
    key = _match_key(name)
    if not key:
        return None
    for f in (doc or load())["funds"]:
        if _match_key(f.get("input_name") or "") == key:
            return f
    return None


def positions(fund: dict) -> dict:
    """ISIN -> % of the fund, for the equity-like sleeve only.

    Rows with no ISIN are dropped rather than keyed on their name: a name key would
    collide "Net Receivables / (Payables)" across two funds and report cash as overlap.
    """
    out: dict = {}
    for h in fund.get("holdings") or []:
        if h.get("kind") in EQUITY_KINDS and h.get("isin"):
            out[h["isin"]] = out.get(h["isin"], 0.0) + float(h.get("weight_pct") or 0.0)
    return out


def names_by_isin(fund: dict) -> dict:
    return {h["isin"]: h.get("name") for h in (fund.get("holdings") or []) if h.get("isin")}


def coverage(doc: dict | None = None) -> dict:
    """What the snapshot can and cannot answer, for the caller to show rather than hide."""
    doc = doc or load()
    funds = doc["funds"]
    ok = [f for f in funds if f.get("status") == "OK"]
    return {
        "generated_at": doc.get("generated_at"),
        "as_of": sorted({f["as_of"] for f in ok if f.get("as_of")}),
        "funds_total": len(funds),
        "funds_available": len(ok),
        "unavailable": [{"fund": f.get("input_name"),
                         "reason": f.get("unavailable_reason")}
                        for f in funds if f.get("status") != "OK"],
    }
