"""Did the agent follow its own evidence?

The divergence check asks whether two portfolios produce different tool sequences. That
rules out a fixed script and nothing more: ARCHITECTURE §2 says so, and measurement agreed —
the sequence varies more between runs than between portfolios, so "they differed" is a
statement about variance as much as about reasoning.

This asks the question the word "agent" actually implies. `attribute_move` ranks every
holding by how much it moved the book. If the agent then drills into a holding, is it the
one the evidence flagged, or a different one?

The flagged holding is chosen BY THE DATA — the largest absolute market_effect among the
contributors the tool returned — never by the author of a test. That is what keeps this
falsifiable rather than decorative.

Three verdicts, because two would lie:

    FOLLOWED    it drilled into the flagged holding
    DIVERGED    it drilled into a different one, which is named
    NO_DRILL    it never drilled into any holding, so this says nothing either way

NO_DRILL is not a quiet pass. An agent that answers from the attribution alone is behaving
reasonably — the decomposition may be the whole answer — but it has not demonstrated the
thing under test, and counting that as success is how an eval ends up measuring nothing.

Pure: no I/O, no SDK, reads a ledger and returns a verdict.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Tools that target ONE holding, and the argument naming it. A tool that looks at the whole
# portfolio (concentration, sharpe, portfolio_snapshot) says nothing about which holding the
# agent chose, so it is not a drill-down.
HOLDING_TOOLS = {
    "holding_units": "symbol",
    "beta": "symbol",
    "price_history": "ticker",
    "get_news": "query",
}

FOLLOWED, DIVERGED, NO_DRILL = "FOLLOWED", "DIVERGED", "NO_DRILL"

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _norm(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


def mentions(holding_name: str, text: str) -> bool:
    """Does `text` name this holding?

    Matched on the holding's FIRST word. A tool is called with a symbol (ICICIBANK.NS)
    while the evidence names it "ICICI Bank", so an exact comparison never fires; and the
    LAST word is usually the shared one — Bank, Fund, Cap, Industries — so matching on it
    would call any bank a match for any other. The first word is the distinctive one.

    Words shorter than three characters are refused rather than guessed at: a two-letter
    key matches far too much to mean anything.
    """
    words = (holding_name or "").split()
    if not words:
        return False
    key = _norm(words[0])
    if len(key) < 3:
        return False
    return key in _norm(text)


def _evidence_order(evidence_id: str) -> int:
    """E12 -> 12. Ledger IDs are issued in call order, so this is 'when'."""
    try:
        return int(str(evidence_id).lstrip("Ee") or 0)
    except ValueError:
        return 0


@dataclass
class Flagged:
    """The holding the evidence pointed at, and where it said so."""
    name: str
    market_effect: float
    evidence_id: str
    runner_up: str | None = None
    runner_up_effect: float = 0.0

    @property
    def decisive(self) -> bool:
        """Is the top contributor clearly ahead of the next one?

        When two holdings moved the book by nearly the same amount, drilling into the
        second is not a failure to follow the evidence — the evidence named both. A
        verdict that ignores this would punish the agent for a coin flip the data handed it.
        """
        if self.runner_up is None:
            return True
        return abs(self.market_effect) > abs(self.runner_up_effect) * 1.25


def flagged_holding(ledger: dict) -> Flagged | None:
    """The biggest mover attribute_move found, over every attribution in the run.

    Returns None when no attribution succeeded — there is then no flag to follow, and the
    caller must not read that as the agent ignoring one.
    """
    ranked: list[tuple[float, str, str]] = []
    for ev in ledger.values():
        if ev.tool != "attribute_move" or ev.error:
            continue
        try:
            body = json.loads(ev.result)
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        for c in body.get("contributors") or []:
            if not isinstance(c, dict) or c.get("status") != "attributed":
                continue
            name = c.get("name")
            try:
                effect = float(c.get("market_effect") or 0.0)
            except (TypeError, ValueError):
                continue
            if name:
                ranked.append((abs(effect), name, ev.id))
    if not ranked:
        return None
    ranked.sort(key=lambda r: -r[0])
    top = ranked[0]
    # The runner-up must be a DIFFERENT holding; the same one appears once per attribution
    # when the agent attributes more than one window.
    nxt = next((r for r in ranked[1:] if r[1] != top[1]), None)
    signed = _signed_effect(ledger, top[2], top[1])
    return Flagged(name=top[1], market_effect=signed, evidence_id=top[2],
                   runner_up=nxt[1] if nxt else None,
                   runner_up_effect=nxt[0] if nxt else 0.0)


def _signed_effect(ledger: dict, evidence_id: str, name: str) -> float:
    """Report the effect as the tool stated it, sign and all, for a readable verdict."""
    ev = ledger.get(evidence_id)
    try:
        for c in json.loads(ev.result).get("contributors") or []:
            if c.get("name") == name:
                return float(c.get("market_effect") or 0.0)
    except Exception:
        pass
    return 0.0


@dataclass
class DrillTarget:
    evidence_id: str
    tool: str
    text: str


def drill_targets(ledger: dict, after: str | None = None) -> list[DrillTarget]:
    """Holding-specific calls, optionally only those made after a given evidence ID.

    Ordering matters: a call made BEFORE the attribution cannot have been prompted by it.
    """
    floor = _evidence_order(after) if after else 0
    out = []
    for ev in sorted(ledger.values(), key=lambda e: _evidence_order(e.id)):
        if _evidence_order(ev.id) <= floor:
            continue
        arg = HOLDING_TOOLS.get(ev.tool)
        if not arg:
            continue
        text = str((ev.args or {}).get(arg) or "")
        if text:
            out.append(DrillTarget(evidence_id=ev.id, tool=ev.tool, text=text))
    return out


@dataclass
class Verdict:
    verdict: str
    detail: str
    flagged: str | None = None
    drilled: list[str] = field(default_factory=list)

    @property
    def followed(self) -> bool:
        return self.verdict == FOLLOWED


def evaluate(ledger: dict, claims: list[str] | None = None) -> Verdict:
    """Did the agent drill into the holding its own evidence flagged?

    `claims` are the verified findings' text. A finding that names the flagged holding
    counts as following the evidence even without a second tool call: concluding from the
    attribution is a legitimate way to act on it, and demanding a tool call would reward
    the agent for spending budget it did not need.
    """
    flag = flagged_holding(ledger)
    if flag is None:
        return Verdict(NO_DRILL, "no successful attribute_move, so nothing was flagged")

    targets = drill_targets(ledger, after=flag.evidence_id)
    if not targets:
        named = [c for c in (claims or []) if mentions(flag.name, c)]
        if named:
            return Verdict(
                FOLLOWED,
                f"no second tool call, but a verified finding names {flag.name!r} "
                f"(market effect {flag.market_effect:+,.2f}, flagged by {flag.evidence_id})",
                flagged=flag.name)
        return Verdict(
            NO_DRILL,
            f"{flag.name!r} moved the book most ({flag.market_effect:+,.2f}) and the agent "
            "drilled into no holding at all, so this run cannot say whether it follows "
            "its evidence", flagged=flag.name)

    hits = [t for t in targets if mentions(flag.name, t.text)]
    drilled = [t.text for t in targets]
    if hits:
        where = ", ".join(f"{t.tool}({t.text})" for t in hits)
        return Verdict(FOLLOWED,
                       f"{flag.name!r} moved the book most ({flag.market_effect:+,.2f}, "
                       f"{flag.evidence_id}) and the agent went to it: {where}",
                       flagged=flag.name, drilled=drilled)

    note = ""
    if not flag.decisive:
        note = (f" NOTE: {flag.runner_up!r} moved it almost as much "
                f"({flag.runner_up_effect:,.2f} absolute), so the evidence named two and "
                "this verdict is weaker than it looks.")
    return Verdict(DIVERGED,
                   f"{flag.name!r} moved the book most ({flag.market_effect:+,.2f}, "
                   f"{flag.evidence_id}) but the agent drilled into {drilled} instead."
                   + note,
                   flagged=flag.name, drilled=drilled)
