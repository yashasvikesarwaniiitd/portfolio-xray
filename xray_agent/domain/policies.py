"""Mechanical safety policies. Pure, deterministic, no model in the loop.

These are the checks that make the product's promises enforceable rather than aspirational:

  violates_guardrail  — no transactional imperative aimed at the user (the SEBI line)
  numbers_are_honest  — every numeral stated must exist in the data it claims to describe
  verify_finding      — a Finding ships only if it cites real evidence and passes both above

`violates_guardrail` and `numbers_are_honest` were written for the Excel health report and
lived in report/insights.py. They moved here because the Investigator needs exactly the same
guarantees, and a safety rule implemented twice drifts. report/insights.py now re-exports
them, so every existing import and test keeps working unchanged.
"""
from __future__ import annotations

import re

# Transactional imperatives aimed at the user. Deterministic, case-insensitive.
_IMPERATIVE = re.compile(
    r"\b(?:you\s+(?:should|must|need\s+to)\s+)?"
    r"(buy(?:ing)?|sell(?:ing)?|exit(?:ing)?|redeem(?:ing)?|liquidat(?:e|ing)|"
    r"offload(?:ing)?|book(?:ing)?\s+profits?|add(?:ing)?\s+more|"
    r"switch(?:ing)?\s+(?:to|into)|trim(?:ming)?|rebalanc(?:e|ing)\s+into)\b",
    re.IGNORECASE)
# Words that appear in legitimate analytics phrasing we must NOT flag.
_SAFE_CONTEXT = re.compile(
    r"\b(?:sell-?off|overbought|oversold|buyback|sell-side|buy-side)\b", re.IGNORECASE)

# Matches grouped numbers BEFORE plain ones, so "175,490.87" is read as one figure and not
# as 175 and 490.87. Both groupings are allowed: Western 175,490.87 and Indian 1,75,490.87
# (groups of two). Without this the verifier rejects any finding that formats money the way
# a human would — found when a model on Groq wrote the amount with separators and had a
# perfectly good finding thrown out, then had to waste a turn restating it unformatted.
_NUM = re.compile(r"-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def _as_float(token: str) -> float:
    return float(token.replace(",", ""))

# Numerals that are part of a NAME, not a claim about the portfolio. "The NIFTY 50 fell"
# states one number, not two, but a naive scan reads the 50 as an unsourced figure and
# rejects a perfectly good finding — which then burns budget on a repair that cannot
# succeed, because 50 was never a claim to begin with. Same idea as _SAFE_CONTEXT above:
# a narrow, deterministic carve-out for known proper nouns. Any OTHER numeral in the same
# sentence is still checked, so this does not weaken the guarantee.
_ENTITY_NUMERALS = re.compile(
    r"\b(?:"
    r"nifty\s*(?:50|100|200|500|next\s*50|midcap\s*\d+|smallcap\s*\d+|bank)"
    r"|sensex(?:\s*30)?"
    r"|s\s*&\s*p\s*(?:500|bse\s*\d+)?"
    r"|bse\s*(?:100|200|500)"
    r"|nasdaq\s*(?:100|composite)?"
    r"|dow\s*(?:jones|30)"
    r"|russell\s*\d+"
    r"|top[-\s]*10"
    r"|\d+\s*y(?:ear)?\s*g-?sec"
    r"|covid[-\s]*19"
    r")\b",
    re.IGNORECASE)


def violates_guardrail(text: str) -> bool:
    """True if the text contains a transactional imperative aimed at the user."""
    cleaned = _SAFE_CONTEXT.sub("", text)
    return bool(_IMPERATIVE.search(cleaned))


def json_numbers(obj) -> set:
    """Every number in a JSON-ish structure, including numerals embedded in strings."""
    out: set[float] = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out.update(_as_float(m) for m in _NUM.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out |= json_numbers(k)
            out |= json_numbers(v)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            out |= json_numbers(v)
    return out


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def _matches_pool(stated: float, decimals: int, pool) -> bool:
    """A stated number matches if some pooled number rounds to it at the stated precision.
    Sign is forgiven: '-4.2%' and 'down 4.2%' describe the same evidence value."""
    return any(abs(round(j, decimals) - stated) < 1e-9
               or abs(abs(round(j, decimals)) - abs(stated)) < 1e-9
               for j in pool)


def claimed_numbers(text: str) -> list[str]:
    """The numerals in `text` that are actually claims, with index and product names
    (NIFTY 50, S&P 500, top-10) removed first so their digits are not mistaken for figures."""
    return [m.group() for m in _NUM.finditer(_ENTITY_NUMERALS.sub(" ", text or ""))]


def numbers_are_honest(text: str, *json_objs) -> bool:
    """Every numeral in `text` must appear in one of the JSON objects, allowing rounding:
    a stated number matches a JSON number that rounds to it at the stated precision."""
    pool = set()
    for o in json_objs:
        pool |= json_numbers(o)
    return not unsupported_numbers(text, pool)


def unsupported_numbers(text: str, pool) -> list[str]:
    """Which numerals in `text` are NOT backed by `pool`. Powers an actionable rejection
    message, so the model is told exactly which figure to drop rather than just 'no'."""
    return [n for n in claimed_numbers(text)
            if not _matches_pool(_as_float(n), _decimals(n), pool)]


def verify_finding(finding, ledger) -> tuple[bool, str | None]:
    """Gate a Finding against the evidence ledger. Returns (ok, rejection_reason).

    Three ways to fail, each with a reason the model can act on:
      1. cites nothing, or cites an evidence ID that does not exist
      2. states a number that appears in none of the cited evidence
      3. contains a transactional imperative

    Note the pool is the CITED evidence only, not the whole ledger — citing E1 and quoting a
    number that only E4 contains is a real sourcing error, and we treat it as one.
    """
    if not finding.evidence_ids:
        return False, ("No evidence cited. Every finding must cite the evidence IDs (E1, E2, "
                       "...) of the tool calls its numbers came from.")

    unknown = [e for e in finding.evidence_ids if e not in ledger]
    if unknown:
        known = ", ".join(sorted(ledger)) or "none yet"
        return False, (f"Unknown evidence ID(s): {', '.join(unknown)}. Available: {known}.")

    failed = [e for e in finding.evidence_ids if not ledger[e].ok]
    if failed:
        return False, (f"Evidence {', '.join(failed)} is a failed tool call and cannot "
                       "support a claim. Cite a successful call or drop the finding.")

    if violates_guardrail(finding.claim):
        return False, ("This reads as a recommendation (buy/sell/exit/switch/trim/add). State "
                       "what the numbers show, or ask the reader a question — never what to do.")

    pool = set()
    for e in finding.evidence_ids:
        pool |= set(ledger[e].numbers)
    bad = unsupported_numbers(finding.claim, pool)
    if bad:
        return False, (f"These numbers are not in the cited evidence: {', '.join(bad)}. Only "
                       "state figures that a tool actually returned, and cite the evidence "
                       "that contains them. Do not compute new numbers yourself.")

    return True, None
