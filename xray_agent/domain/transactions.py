"""Canonical transaction model: dated buys and sells, with the maths they enable.

Why this exists. The original input was a hand-maintained sheet of monthly SIP amounts, so
units had to be RECONSTRUCTED: units = amount / price-on-the-1st-of-that-month. That was
the best available given the input, and it is still what `agent.reconstruct_holding` does.
A broker order statement is strictly better information — the real trade date, the real
quantity, the real executed price — so when we have it we should stop inferring and just
read it.

It also fixes a gap the SIP sheet could never express: **sells**. Nothing in the original
engine modelled one. Every flow was positive and `units_held_at` simply summed them, which
is fine for pure accumulation and wrong for any real portfolio.

THE DESIGN CHOICE THAT MAKES THIS CHEAP: rather than teach every downstream metric about a
new type, `to_priced_flows` maps transactions into the `{date, amount, price}` shape the
engine already speaks, with a SELL carried as a negative amount. `metrics.units_held_at`
computes `amount / price`, so a negative amount yields negative units and position tracking
through a sell works with no change to that function, to `attribute_move`, or to the
portfolio timeline. The attribution identity also survives it unchanged, because its
derivation only ever used units and prices:

    V1 - V0 = u0.(p1 - p0) + (u1 - u0).p1

holds whether units went up or down. What changes is only the LABEL: `new_money` becomes
net new money, buys less sell proceeds, so `attribute_move` reports gross buys and sells
separately rather than letting a sale masquerade as a withdrawal of market value.

Pure: no I/O, no SDK, no parsing. Reading files is an adapter's job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

BUY, SELL = "BUY", "SELL"


@dataclass(frozen=True)
class Transaction:
    """One executed trade. `units` is always positive; `side` carries the direction."""
    date: date
    side: str
    units: float
    price: float
    symbol: str = ""
    name: str = ""

    @property
    def signed_units(self) -> float:
        return self.units if self.side == BUY else -self.units

    @property
    def value(self) -> float:
        """Rupees that changed hands, always positive."""
        return self.units * self.price

    @property
    def signed_value(self) -> float:
        """Positive for money going in, negative for proceeds coming back out."""
        return self.signed_units * self.price


def sort_txns(txns: list) -> list:
    """Chronological, with buys before sells on the same day.

    The tie-break matters: a report that lists a same-day sale first would otherwise look
    like selling stock you do not yet hold, and FIFO matching would raise on it.
    """
    return sorted(txns, key=lambda t: (t.date, 0 if t.side == BUY else 1))


def units_at(txns: list, as_of: date) -> float:
    """Net units held at the close of `as_of`."""
    return sum(t.signed_units for t in txns if t.date <= as_of)


def to_priced_flows(txns: list) -> list:
    """Transactions in the `{date, amount, price}` shape the existing engine speaks.

    A sell becomes a NEGATIVE amount at a positive price, so `metrics.units_held_at`
    (which divides amount by price) returns negative units for it and every date-aware
    metric handles a reducing position without being modified.
    """
    return [{"date": t.date, "amount": t.signed_value, "price": t.price,
             "side": t.side, "units": t.signed_units}
            for t in sort_txns(txns) if t.price > 0]


def realised_pnl(txns: list) -> dict:
    """FIFO-matched realised profit on closed quantity.

    FIFO because that is how Indian capital gains are computed, so the number lines up
    with what the taxpayer will actually be assessed on. Brokers show AVERAGE cost on the
    holdings screen, which is a different number for a different purpose — this is not
    tax advice, it is the same arithmetic the tax rules prescribe.

    A sell with no matching buy in the file (shares transferred in, or a statement that
    starts mid-history) is reported in `unmatched_units` rather than dropped or guessed.
    """
    lots: list = []          # open buy lots: [units, price]
    realised = 0.0
    proceeds = 0.0
    cost_of_sold = 0.0
    unmatched = 0.0
    closed: list = []

    for t in sort_txns(txns):
        if t.side == BUY:
            lots.append([t.units, t.price])
            continue
        remaining = t.units
        proceeds += t.value
        while remaining > 1e-12 and lots:
            lot = lots[0]
            take = min(lot[0], remaining)
            gain = take * (t.price - lot[1])
            realised += gain
            cost_of_sold += take * lot[1]
            closed.append({"units": round(take, 6), "buy_price": round(lot[1], 4),
                           "sell_price": round(t.price, 4), "sell_date": t.date.isoformat(),
                           "gain": round(gain, 2)})
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                lots.pop(0)
        if remaining > 1e-12:
            unmatched += remaining

    open_units = sum(u for u, _ in lots)
    open_cost = sum(u * p for u, p in lots)
    out = {
        "realised_pnl": round(realised, 2),
        "sale_proceeds": round(proceeds, 2),
        "cost_of_units_sold": round(cost_of_sold, 2),
        "open_units": round(open_units, 6),
        "open_cost": round(open_cost, 2),
        "closed_lots": closed,
    }
    if open_units > 1e-12:
        out["average_cost"] = round(open_cost / open_units, 4)
    if unmatched > 1e-12:
        out["unmatched_units"] = round(unmatched, 6)
        out["unmatched_note"] = ("units sold with no matching purchase in this file — the "
                                 "statement may start mid-history, or the holding was "
                                 "transferred in. Realised P&L on those units is unknown "
                                 "and is excluded, never guessed.")
    return out


def cashflows_for_xirr(txns: list, as_of: date, current_price: float | None) -> list:
    """(date, amount) pairs for a money-weighted return.

    Sign convention: money leaving the investor is negative, money coming back positive.
    So buys are negative, SALE PROCEEDS ARE POSITIVE (getting that wrong is the classic
    XIRR bug — a profitable sale reads as a loss), and the final open position is valued
    at `current_price` as a closing inflow.
    """
    flows = [(t.date, -t.signed_value) for t in sort_txns(txns) if t.date <= as_of]
    if current_price:
        held = units_at(txns, as_of)
        if abs(held) > 1e-12:
            flows.append((as_of, held * current_price))
    return flows


def summarise(txns: list, as_of: date, current_price: float | None) -> dict:
    """Everything derivable from one holding's transactions. Never raises."""
    txns = sort_txns(txns)
    if not txns:
        return {"status": "no transactions"}

    held = units_at(txns, as_of)
    pnl = realised_pnl([t for t in txns if t.date <= as_of])
    buys = sum(t.value for t in txns if t.side == BUY and t.date <= as_of)
    sells = sum(t.value for t in txns if t.side == SELL and t.date <= as_of)

    out = {
        "units": round(held, 6),
        "gross_invested": round(buys, 2),
        "gross_sold": round(sells, 2),
        "net_invested": round(buys - sells, 2),
        "realised_pnl": pnl["realised_pnl"],
        "open_cost": pnl["open_cost"],
        "first_trade": txns[0].date.isoformat(),
        "last_trade": txns[-1].date.isoformat(),
        "trade_count": len(txns),
        "buy_count": sum(1 for t in txns if t.side == BUY),
        "sell_count": sum(1 for t in txns if t.side == SELL),
    }
    if "average_cost" in pnl:
        out["average_cost"] = pnl["average_cost"]
    if "unmatched_units" in pnl:
        out["unmatched_units"] = pnl["unmatched_units"]
        out["unmatched_note"] = pnl["unmatched_note"]
    if current_price and abs(held) > 1e-12:
        value = held * current_price
        out["current_price"] = round(current_price, 4)
        out["current_value"] = round(value, 2)
        out["unrealised_pnl"] = round(value - pnl["open_cost"], 2)
        out["total_pnl"] = round(out["unrealised_pnl"] + pnl["realised_pnl"], 2)
    elif abs(held) <= 1e-12:
        out["status"] = "fully exited"
        out["total_pnl"] = pnl["realised_pnl"]
    return out
