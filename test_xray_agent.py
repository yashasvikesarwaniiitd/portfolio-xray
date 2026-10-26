"""Hermetic tests for the Investigator: no network, no API key, no clock dependence.

Every attribution case below is hand-computed in the comment above it. If the code and the
comment disagree, the code is wrong — that is the same rule the Parag Parikh regression in
test_agent.py enforces for the reconstruction engine.
"""
from datetime import date

import pytest

from xray_agent.domain import policies
from xray_agent.domain.attribution import attribute_move
from xray_agent.domain.models import Evidence, Finding


def _flow(d: date, amount: float, price: float | None) -> dict:
    return {"date": d, "amount": amount, "price": price}


# A holding that bought 100 units at 100 in January, priced 110 on 1 Mar and 150 on 1 Jun.
_PMAP = {date(2025, 1, 1): 100.0, date(2025, 3, 1): 110.0,
         date(2025, 4, 1): 120.0, date(2025, 6, 1): 150.0}
START, END = date(2025, 3, 1), date(2025, 6, 1)


# --- attribution ------------------------------------------------------------------------

def test_no_new_money_is_pure_market_effect():
    """100 units, 110 -> 120 over the window, nothing bought.
         V0 = 100 x 110 = 11,000      V1 = 100 x 120 = 12,000     change = +1,000
         market_effect = 100 x (120 - 110) = 1,000; new money and its gain are 0."""
    pmap = {date(2025, 1, 1): 100.0, date(2025, 3, 1): 110.0, date(2025, 6, 1): 120.0}
    h = {"name": "A", "price_map": pmap,
         "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0)]}
    out = attribute_move([h], START, END)

    assert out["value_start"] == 11_000.00
    assert out["value_end"] == 12_000.00
    assert out["change"] == 1_000.00
    assert out["market_effect"] == 1_000.00
    assert out["new_money"] == 0.0
    assert out["gain_on_new_money"] == 0.0
    assert out["identity_residual"] == 0.0
    assert out["coverage_pct"] == 100.0
    # 120/110 - 1 = 9.0909...%
    assert out["contributors"][0]["price_change_pct"] == 9.09
    assert out["return_on_opening_capital_pct"] == 9.09


def test_mid_window_sip_is_separated_from_market_effect():
    """The case the naive 'compare two balances' answer gets wrong.
         Jan: 10,000 at 100 -> 100 units.   Apr (inside window): 6,000 at 120 -> 50 units.
         u0 = 100 @ 110 = 11,000      u1 = 150 @ 150 = 22,500      change = +11,500
         market_effect     = 100 x (150 - 110)      =  4,000
         new_money         = 6,000 (the April SIP)  =  6,000
         gain_on_new_money = 50 x 150 - 6,000       =  1,500
                                                      ------
                                                      11,500  <- exactly the change
    Only 5,500 of the 11,500 rise was performance; 6,000 was just money arriving."""
    h = {"name": "A", "price_map": _PMAP,
         "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0),
                          _flow(date(2025, 4, 1), 6_000, 120.0)]}
    out = attribute_move([h], START, END)

    assert out["change"] == 11_500.00
    assert out["market_effect"] == 4_000.00
    assert out["new_money"] == 6_000.00
    assert out["gain_on_new_money"] == 1_500.00
    assert out["gain"] == 5_500.00
    assert out["identity_residual"] == 0.0
    # 4,000 earned on the 11,000 that was already invested.
    assert out["return_on_opening_capital_pct"] == 36.36


def test_bought_entirely_inside_the_window_has_no_market_effect():
    """Nothing held at the start, so there is no opening capital to earn a market effect on.
         u0 = 0, u1 = 50 @ 150 = 7,500 = 6,000 new money + 1,500 gain on it."""
    pmap = {date(2025, 4, 1): 120.0, date(2025, 6, 1): 150.0}
    h = {"name": "A", "price_map": pmap,
         "priced_flows": [_flow(date(2025, 4, 1), 6_000, 120.0)]}
    out = attribute_move([h], START, END)

    assert out["value_start"] == 0.0
    assert out["market_effect"] == 0.0
    assert out["new_money"] == 6_000.00
    assert out["gain_on_new_money"] == 1_500.00
    assert out["change"] == 7_500.00
    assert out["identity_residual"] == 0.0
    assert "return_on_opening_capital_pct" not in out  # nothing to divide by


def test_unpriced_sip_does_not_invent_a_phantom_loss():
    """6,000 arrived but could not be priced, so it bought no reconstructable units.
    Counting it as new_money would break the identity and show a 6,000 'loss'. It is
    reported separately instead, and the identity still closes on the priced part."""
    h = {"name": "A", "price_map": {date(2025, 3, 1): 110.0, date(2025, 6, 1): 150.0},
         "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0),
                          _flow(date(2025, 4, 1), 6_000, None)]}
    out = attribute_move([h], START, END)

    assert out["new_money"] == 0.0
    assert out["market_effect"] == 4_000.00        # 100 x (150 - 110)
    assert out["gain_on_new_money"] == 0.0
    assert out["change"] == 4_000.00
    assert out["identity_residual"] == 0.0
    assert out["contributors"][0]["new_money_unpriced"] == 6_000.00


def test_unpriceable_holding_is_reported_not_hidden():
    """A manual basket has no price source. It must not silently vanish from the total:
    it lands in `uncovered` and drags coverage below 100%.
         covered end value 12,000; uncovered invested 5,000 -> 12,000/17,000 = 70.6%"""
    pmap = {date(2025, 3, 1): 110.0, date(2025, 6, 1): 120.0}
    priced = {"name": "A", "price_map": pmap,
              "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0)]}
    manual = {"name": "Manual Basket", "price_map": {}, "priced_flows": [], "invested": 5_000}
    out = attribute_move([priced, manual], START, END)

    assert out["holdings_attributed"] == 1
    assert out["holdings_uncovered"] == 1
    assert out["coverage_pct"] == 70.6
    assert out["uncovered"][0]["name"] == "Manual Basket"
    assert "no price history" in out["uncovered"][0]["reason"]


def test_holding_with_units_but_no_start_price_is_uncovered():
    """Units were held at the window start but the price map has nothing within the
    walk-back, so the opening value is unknowable. We refuse to guess it."""
    h = {"name": "A", "price_map": {date(2025, 1, 1): 100.0, date(2025, 6, 1): 150.0},
         "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0)]}
    out = attribute_move([h], START, END)

    assert out["holdings_uncovered"] == 1
    assert "cannot value the window start" in out["uncovered"][0]["reason"]


def test_benchmark_compares_like_for_like():
    """The portfolio side of the comparison excludes new money, so a mid-window SIP cannot
    flatter it. Index 20,000 -> 22,000 = +10.00%; portfolio opening capital earned 36.36%."""
    h = {"name": "A", "price_map": _PMAP,
         "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0),
                          _flow(date(2025, 4, 1), 6_000, 120.0)]}
    bm = {date(2025, 3, 1): 20_000.0, date(2025, 6, 1): 22_000.0}
    out = attribute_move([h], START, END, benchmark_price_map=bm)

    assert out["benchmark"]["return_pct"] == 10.00
    assert out["benchmark"]["vs_portfolio_pp"] == 26.36   # 36.36 - 10.00
    assert out["return_on_opening_capital_pct"] == 36.36


def test_contributors_are_ranked_by_absolute_gain():
    pmap_up = {date(2025, 3, 1): 100.0, date(2025, 6, 1): 110.0}      # +1,000 on 100 units
    pmap_down = {date(2025, 3, 1): 100.0, date(2025, 6, 1): 70.0}     # -3,000 on 100 units
    small = {"name": "Small", "price_map": pmap_up,
             "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0)]}
    big = {"name": "Big", "price_map": pmap_down,
           "priced_flows": [_flow(date(2025, 1, 1), 10_000, 100.0)]}
    out = attribute_move([small, big], START, END)

    assert [c["name"] for c in out["contributors"]] == ["Big", "Small"]
    assert out["contributors"][0]["gain"] == -3_000.00


def test_backwards_window_raises():
    with pytest.raises(ValueError, match="must be after"):
        attribute_move([], END, START)


def test_empty_portfolio_does_not_divide_by_zero():
    out = attribute_move([], START, END)
    assert out["change"] == 0.0
    assert out["coverage_pct"] == 100.0
    assert out["identity_residual"] == 0.0


# --- finding verification ----------------------------------------------------------------

def _ev(eid: str, result: str, error: str | None = None) -> Evidence:
    return Evidence(id=eid, tool="attribute_move", args={}, result=result,
                    numbers=frozenset(policies.json_numbers(result)), error=error)


_LEDGER = {
    "E1": _ev("E1", '{"change": -12450.5, "market_effect": -18200.0, "new_money": 5749.5}'),
    "E2": _ev("E2", '{"name": "Reliance", "gain": -8100.25}'),
    "E3": _ev("E3", "Error: no such symbol", error="no such symbol"),
}


def test_finding_with_sourced_numbers_is_verified():
    f = Finding(claim="The portfolio fell 12450.5, of which -18200.0 was market effect.",
                evidence_ids=["E1"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert ok and reason is None


def test_finding_citing_nothing_is_rejected():
    ok, reason = policies.verify_finding(Finding(claim="Things went down.", evidence_ids=[]),
                                         _LEDGER)
    assert not ok and "No evidence cited" in reason


def test_finding_citing_unknown_evidence_is_rejected():
    f = Finding(claim="It fell 12450.5.", evidence_ids=["E9"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert not ok and "E9" in reason


def test_finding_citing_a_failed_tool_call_is_rejected():
    f = Finding(claim="Nothing to see.", evidence_ids=["E3"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert not ok and "failed tool call" in reason


def test_finding_with_an_unsourced_number_is_rejected_and_names_it():
    """99.9 appears nowhere in E1. The rejection must say which number, so the model can
    repair the claim instead of guessing what upset the verifier."""
    f = Finding(claim="The portfolio fell 12450.5, a 99.9% collapse.", evidence_ids=["E1"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert not ok and "99.9" in reason


def test_number_must_be_in_the_CITED_evidence_not_merely_somewhere():
    """-8100.25 is real, but it lives in E2 and the finding only cites E1. Sourcing is
    per-claim, not per-run."""
    f = Finding(claim="Reliance lost 8100.25.", evidence_ids=["E1"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert not ok and "8100.25" in reason

    ok2, _ = policies.verify_finding(Finding(claim="Reliance lost 8100.25.",
                                             evidence_ids=["E2"]), _LEDGER)
    assert ok2


def test_finding_that_recommends_an_action_is_rejected():
    f = Finding(claim="Reliance lost 8100.25, so you should sell it.", evidence_ids=["E2"])
    ok, reason = policies.verify_finding(f, _LEDGER)
    assert not ok and "recommendation" in reason


def test_rounding_is_allowed_but_invention_is_not():
    """The rule is: round the EVIDENCE value to the precision the text states, then compare.
    Python's round() is half-to-even, so -12450.5 at 0 decimals is -12450, not -12451. That
    is strict in the safe direction — a model that rounds the other way gets rejected and
    retries — and it is the same rule the Excel report has shipped with, so it stays."""
    assert policies.numbers_are_honest("down 12450.5", {"change": -12450.5})
    assert policies.numbers_are_honest("about 12450", {"change": -12450.5})
    assert not policies.numbers_are_honest("about 12451", {"change": -12450.5})
    assert not policies.numbers_are_honest("down 12460", {"change": -12450.5})
    # Sign is forgiven: "fell 12450.5" describes a change of -12450.5.
    assert policies.numbers_are_honest("fell 12450.5", {"change": -12450.5})
