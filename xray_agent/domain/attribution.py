"""Decompose a portfolio's change in value over a window into causes.

This is the analytical heart of "Explain my move". The naive version — compare value today
with value a month ago — is wrong in a SIP portfolio, because money you *added* during the
window raises the value without anything having gone up. Separating those is the whole job.

For one holding over the window (start, end], with u = units and p = price:

    market_effect     = u0 x (p1 - p0)          gain on units you already held
    new_money         = sum of priced SIPs in the window
    gain_on_new_money = (u1 - u0) x p1 - new_money

and these are not an approximation -- they sum exactly:

    market_effect + new_money + gain_on_new_money
      = u0.p1 - u0.p0 + (u1 - u0).p1
      = u1.p1 - u0.p0
      = V1 - V0                                  (the identity, checked at runtime)

So the per-holding residual is always zero by construction. Any portfolio-level gap comes
only from holdings we could not price at one end of the window, and those are reported as
uncovered with a reason rather than quietly folded into the total.

Two honesty details that matter:
  * An unpriced SIP creates no units, so counting its rupees as `new_money` would break the
    identity and invent a phantom loss. Those rupees are reported as `new_money_unpriced`.
  * The index comparison uses `market_effect / value_start`, the return on capital that was
    already invested. Comparing total change against an index would flatter or punish the
    portfolio purely for the timing of a SIP.

Pure: no network, no clock, no SDK. `metrics` is itself pure, network-free math, so the
dependency rule is satisfied -- we reuse `units_held_at`/`nav_on_or_before` rather than
re-implement the exact-reconstruction rules in a second place.
"""
from __future__ import annotations

from datetime import date

import metrics

# Holidays can close a market for several consecutive days (Diwali, Christmas-New Year), and
# AMFI publishes no NAV on non-business days, so a month boundary can sit a week from the
# nearest published price. 15 days is the same buffer agent.reconstruct_holding uses.
WALK_BACK_DAYS = 15


def _window_flows(flows: list[dict], start: date, end: date) -> tuple[float, float]:
    """(priced, unpriced) rupees that arrived in (start, end]. Half-open at the start so a
    SIP dated exactly on `start` counts as already-held capital, not new money."""
    priced = unpriced = 0.0
    for f in flows:
        if not (start < f["date"] <= end):
            continue
        if f.get("price"):
            priced += f["amount"]
        else:
            unpriced += f["amount"]
    return priced, unpriced


def attribute_holding(h: dict, start: date, end: date,
                      max_back: int = WALK_BACK_DAYS) -> dict:
    """Decompose one holding's change. `h` needs name, price_map and priced_flows — exactly
    what agent.reconstruct_holding(capture=...) hands back. Never raises."""
    name = h.get("name", "(unnamed)")
    pmap = h.get("price_map") or {}
    flows = h.get("priced_flows") or []
    invested = float(h.get("invested") or sum(f["amount"] for f in flows))

    def uncovered(reason: str) -> dict:
        return {"name": name, "status": "uncovered", "reason": reason,
                "invested": round(invested, 2)}

    if not pmap or not flows:
        return uncovered("no price history or no priced SIPs for this holding")

    u0 = metrics.units_held_at(flows, start)
    u1 = metrics.units_held_at(flows, end)
    p1 = metrics.nav_on_or_before(pmap, end, max_back=max_back)
    if p1 is None:
        return uncovered(f"no price on or before {end} (within {max_back} days)")

    if u0 > 0:
        p0 = metrics.nav_on_or_before(pmap, start, max_back=max_back)
        if p0 is None:
            return uncovered(f"held units at {start} but no price then "
                             f"(within {max_back} days) — cannot value the window start")
    else:
        # Nothing held at the start, so the opening value is zero whatever the price was.
        p0 = 0.0

    new_money, new_money_unpriced = _window_flows(flows, start, end)
    value_start = u0 * p0
    value_end = u1 * p1
    market_effect = u0 * (p1 - p0) if u0 > 0 else 0.0
    gain_on_new_money = (u1 - u0) * p1 - new_money

    row = {
        "name": name,
        "status": "attributed",
        "units_start": round(u0, 4),
        "units_end": round(u1, 4),
        "price_start": round(p0, 4) if u0 > 0 else None,
        "price_end": round(p1, 4),
        "value_start": round(value_start, 2),
        "value_end": round(value_end, 2),
        "change": round(value_end - value_start, 2),
        "market_effect": round(market_effect, 2),
        "new_money": round(new_money, 2),
        "gain_on_new_money": round(gain_on_new_money, 2),
        # What the investment actually earned over the window, with the mechanical effect of
        # adding money stripped out. This is the number that answers "why am I down".
        "gain": round(market_effect + gain_on_new_money, 2),
    }
    # Unrounded values for aggregation. Summing the DISPLAY-rounded figures above would
    # accumulate a cent of error per holding and show a non-zero identity residual that is
    # pure presentation noise — which invites exactly the wrong question about an identity
    # that is meant to be exact. Popped before the row is handed to the model.
    row["_exact"] = {"value_start": value_start, "value_end": value_end,
                     "market_effect": market_effect, "new_money": new_money,
                     "gain_on_new_money": gain_on_new_money}
    if u0 > 0 and p0:
        row["price_change_pct"] = round((p1 / p0 - 1) * 100, 2)
    if new_money_unpriced:
        row["new_money_unpriced"] = round(new_money_unpriced, 2)
        row["new_money_unpriced_note"] = ("rupees that arrived in the window but could not be "
                                          "priced, so they bought no reconstructable units "
                                          "and are excluded from the decomposition")
    return row


def attribute_move(holdings: list[dict], start: date, end: date,
                   benchmark_price_map: dict | None = None,
                   benchmark_name: str = "NIFTY 50",
                   max_back: int = WALK_BACK_DAYS) -> dict:
    """Decompose the whole portfolio's move over (start, end].

    Returns per-holding rows, portfolio totals, an index comparison on a like-for-like basis,
    the coverage share, and an identity residual that should always be 0.00 — a self-check
    printed rather than hidden, so a bug in this function is visible in the output.
    """
    if end <= start:
        raise ValueError(f"window end ({end}) must be after window start ({start})")

    rows = [attribute_holding(h, start, end, max_back=max_back) for h in holdings]
    done = [r for r in rows if r["status"] == "attributed"]
    gaps = [r for r in rows if r["status"] == "uncovered"]

    exact = [r.pop("_exact") for r in done]
    value_start = sum(e["value_start"] for e in exact)
    value_end = sum(e["value_end"] for e in exact)
    market_effect = sum(e["market_effect"] for e in exact)
    new_money = sum(e["new_money"] for e in exact)
    gain_on_new_money = sum(e["gain_on_new_money"] for e in exact)
    change = value_end - value_start

    uncovered_invested = sum(r["invested"] for r in gaps)
    denom = value_end + uncovered_invested
    coverage_pct = round(value_end / denom * 100, 1) if denom > 0 else 100.0

    out = {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": (end - start).days},
        "value_start": round(value_start, 2),
        "value_end": round(value_end, 2),
        "change": round(change, 2),
        "market_effect": round(market_effect, 2),
        "new_money": round(new_money, 2),
        "gain_on_new_money": round(gain_on_new_money, 2),
        "gain": round(market_effect + gain_on_new_money, 2),
        # Should be 0.0. Computed, not asserted away: if it ever isn't, the output says so.
        "identity_residual": round(change - (market_effect + new_money + gain_on_new_money), 2),
        "coverage_pct": coverage_pct,
        "holdings_attributed": len(done),
        "holdings_uncovered": len(gaps),
        "uncovered": gaps,
        # Biggest absolute movers first — the model needs the drivers, not the alphabet.
        "contributors": sorted(done, key=lambda r: -abs(r["gain"])),
        "method": ("change = market_effect + new_money + gain_on_new_money, exactly. "
                   "market_effect = units held at start x price change; new_money = priced "
                   "SIPs inside the window; gain_on_new_money = what those new units are "
                   "worth now, less what was paid for them."),
    }

    if value_start > 0:
        # Return on capital already invested at the window start. This EXCLUDES new money,
        # which is what makes it comparable to an index return over the same dates.
        out["return_on_opening_capital_pct"] = round(market_effect / value_start * 100, 2)

    if benchmark_price_map:
        b0 = metrics.nav_on_or_before(benchmark_price_map, start, max_back=max_back)
        b1 = metrics.nav_on_or_before(benchmark_price_map, end, max_back=max_back)
        if b0 and b1:
            bench_pct = round((b1 / b0 - 1) * 100, 2)
            out["benchmark"] = {
                "name": benchmark_name,
                "start_level": round(b0, 2),
                "end_level": round(b1, 2),
                "return_pct": bench_pct,
            }
            if "return_on_opening_capital_pct" in out:
                out["benchmark"]["vs_portfolio_pp"] = round(
                    out["return_on_opening_capital_pct"] - bench_pct, 2)
                out["benchmark"]["basis"] = (
                    "portfolio figure is the return on capital already invested at the window "
                    "start, so new money does not distort the comparison")
        else:
            out["benchmark"] = {"name": benchmark_name,
                                "error": "no index level available at one end of the window"}

    return out
