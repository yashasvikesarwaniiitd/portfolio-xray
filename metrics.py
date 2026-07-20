"""Pure, network-free financial math for Portfolio X-Ray.

Every function here is a deterministic pure function of its inputs — no I/O, no
global state — so each is unit-testable in isolation against hand-verified values.
The LLM never runs this math; the tools in agent.py fetch data and call these.

Conventions:
- XIRR uses a 365-day year and a pure-Python bisection solver (no scipy).
- Unit reconstruction is EXACT: units bought per SIP = amount / price-on-date,
  summed across SIPs — not an average-cost approximation.
"""
import math
from datetime import date, timedelta


def nav_on_or_before(price_map: dict, target: date, max_back: int = 10):
    """Return the price on `target`, walking back up to `max_back` days to skip
    weekends/holidays with no published price. None if nothing is found in range."""
    d = target
    for _ in range(max_back + 1):
        if d in price_map:
            return price_map[d]
        d -= timedelta(days=1)
    return None


def reconstruct_units(priced_flows: list[dict]) -> dict:
    """EXACT unit reconstruction. `priced_flows` is a list of
    {"date": date, "amount": float, "price": float|None}; units bought = amount/price.
    Flows with no price (SIP before the instrument existed) contribute no units and are
    reported separately so invested stays consistent with the units actually reconstructed.

    Returns total_units, invested_priced (sum of amounts we could price), invested_unpriced,
    and a per-SIP breakdown."""
    total_units = 0.0
    invested_priced = 0.0
    invested_unpriced = 0.0
    breakdown = []
    for f in priced_flows:
        amount, price = f["amount"], f.get("price")
        if price and price > 0:
            units = amount / price
            total_units += units
            invested_priced += amount
            breakdown.append({"date": str(f["date"]), "amount": round(amount, 2),
                              "price": round(price, 4), "units": round(units, 4)})
        else:
            invested_unpriced += amount
            breakdown.append({"date": str(f["date"]), "amount": round(amount, 2),
                              "price": None, "units": None})
    return {
        "total_units": total_units,
        "invested_priced": round(invested_priced, 2),
        "invested_unpriced": round(invested_unpriced, 2),
        "priced_sips": sum(1 for f in priced_flows if f.get("price")),
        "total_sips": len(priced_flows),
        "breakdown": breakdown,
    }


def xirr(cashflows: list[tuple], low: float = -0.9999, high: float = 10.0,
         tol: float = 1e-7, max_iter: int = 200):
    """Annualized internal rate of return for dated cashflows via bisection (no scipy).

    `cashflows` is a list of (date, amount): outflows negative, inflows positive; the
    current value is the final positive flow. Returns the rate as a fraction (0.069 = 6.9%).
    Raises ValueError if the sign convention is wrong or no root is bracketed in [low, high]."""
    if len(cashflows) < 2:
        raise ValueError("XIRR needs at least two cashflows")
    amounts = [cf for _, cf in cashflows]
    if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
        raise ValueError("XIRR needs at least one negative and one positive cashflow")
    d0 = min(d for d, _ in cashflows)

    def npv(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** ((d - d0).days / 365.0) for d, cf in cashflows)

    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        raise ValueError("XIRR: no sign change in bracket; rate outside [%.4f, %.1f]"
                         % (low, high))
    lo, hi = low, high
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) / 2.0 < tol:
            return mid
        if f_low * f_mid < 0:
            hi = mid
        else:
            lo, f_low = mid, f_mid
    return (lo + hi) / 2.0


def regression_slope(y: list[float], x: list[float]) -> float:
    """Ordinary-least-squares slope of y on x = cov(x, y) / var(x). This is beta when
    y = holding daily returns and x = market daily returns."""
    n = len(x)
    if n < 2 or len(y) != n:
        raise ValueError("regression_slope needs equal-length series of length >= 2")
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    var = sum((xi - mx) ** 2 for xi in x)
    if var == 0:
        raise ValueError("regression_slope: market variance is zero")
    return cov / var


def daily_returns(prices: list[float]) -> list[float]:
    """Simple day-over-day returns from a price series (length n -> n-1 returns)."""
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))
            if prices[i - 1]]


def annualize(returns: list[float], periods_per_year: int = 252) -> dict:
    """Annualized mean return and volatility from a periodic (daily) return series.
    Volatility uses the sample standard deviation. Returns fractions, not percents."""
    n = len(returns)
    if n < 2:
        raise ValueError("annualize needs at least two returns")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)  # sample variance
    std = math.sqrt(var)
    return {
        "ann_return": mean * periods_per_year,
        "ann_volatility": std * math.sqrt(periods_per_year),
        "n_obs": n,
    }


def sharpe_ratio(ann_return: float, ann_volatility: float, risk_free_rate: float) -> float:
    """(annualized return - risk-free rate) / annualized volatility. All fractions."""
    if ann_volatility == 0:
        raise ValueError("Sharpe undefined: zero volatility")
    return (ann_return - risk_free_rate) / ann_volatility


def concentration_stats(holdings: list[dict], flag_threshold: float = 0.10) -> dict:
    """Portfolio concentration from priced holdings. Each holding is
    {"name", "value", "market", "type", "risk"}. Computes weight per holding, HHI
    (sum of squared fractional weights, 1/n .. 1), weight by Market/Type/Risk, and flags
    any single holding over `flag_threshold` (default 10%)."""
    total = sum(h["value"] for h in holdings)
    if total <= 0:
        raise ValueError("concentration_stats: total value must be positive")
    weighted = []
    for h in holdings:
        w = h["value"] / total
        weighted.append({"name": h["name"], "value": round(h["value"], 2),
                         "weight_pct": round(w * 100, 2)})
    weighted.sort(key=lambda r: r["weight_pct"], reverse=True)
    hhi = sum((h["value"] / total) ** 2 for h in holdings)

    def group(key: str) -> dict:
        out: dict[str, float] = {}
        for h in holdings:
            out[h.get(key) or "(unspecified)"] = out.get(h.get(key) or "(unspecified)", 0.0) \
                + h["value"] / total * 100
        return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}

    flags = [h for h in weighted if h["weight_pct"] > flag_threshold * 100]
    return {
        "holdings_by_weight": weighted,
        "hhi": round(hhi, 4),
        "effective_holdings": round(1.0 / hhi, 2),  # 1/HHI = effective number of positions
        "by_market": group("market"),
        "by_type": group("type"),
        "by_risk": group("risk"),
        "over_threshold": flags,
        "flag_threshold_pct": round(flag_threshold * 100, 2),
    }
