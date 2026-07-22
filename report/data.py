"""Section data assembly for the health report — ALL deterministic Python.

One function per report section returns a JSON-serializable dict of that section's computed
numbers, built from the existing pipeline (read_portfolio, reconstruct_holding, metrics).
The AI layer only ever restates these numbers; it never computes.

Market metadata (sector, market cap, expense ratio, fund top-10 holdings) comes from
yfinance and is cached to report/.cache.json (gitignored) so a report re-run doesn't
refetch. Every fetch degrades gracefully — a section renders with an honest
"data unavailable" note rather than crashing the report.

Input levels (detected from the data present, never hardcoded):
  L0 — holdings identified (symbols) but no amounts   -> overlap only
  L1 — amounts invested present (weights derivable)   -> + allocation, diversification,
                                                          concentration, sector, questions
  L2 — monthly SIP amounts present (values/XIRR real) -> + risk, cost (current-value based)
"""
import json
import os
from datetime import date, timedelta

import yfinance as yf

import agent
import metrics

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.json")

LEVELS = {"L0": 0, "L1": 1, "L2": 2}

# Balanced reference shape for the radar (from the approved reference design).
RADAR_REFERENCE = [("Equity", 55), ("Debt", 20), ("Gold", 10),
                   ("International", 15), ("Cash/Alt", 0)]


# --- input level -----------------------------------------------------------------------

def detect_level(holdings: list[dict]) -> str:
    if any(h["inflows"] for h in holdings):
        return "L2"
    if any(h["total_invested"] > 0 for h in holdings):
        return "L1"
    return "L0"


# --- market-metadata cache (yfinance; free, no API key) --------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def fetch_meta(symbol: str, cache: dict) -> dict:
    """Sector / marketCap / currency / expense ratio / quoteType / fund top-10 holdings for
    a yfinance symbol, cached for ~7 days. Missing fields stay None — never raises."""
    ent = cache.get(symbol)
    if ent and (date.today() - date.fromisoformat(ent.get("ts", "2000-01-01"))).days < 7:
        return ent
    meta = {"sector": None, "market_cap": None, "currency": None, "expense_ratio_pct": None,
            "quote_type": None, "top_holdings": None, "ts": date.today().isoformat()}
    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        meta["sector"] = info.get("sector")
        meta["market_cap"] = info.get("marketCap")
        meta["currency"] = info.get("currency")
        meta["quote_type"] = info.get("quoteType")
        # yfinance reports TER in percent form (e.g. 0.03 for IVV's 0.03%).
        er = info.get("netExpenseRatio", info.get("annualReportExpenseRatio"))
        if er is not None:
            meta["expense_ratio_pct"] = round(float(er), 3)
        try:  # ETF/fund disclosed top holdings (name + % of fund)
            th = t.funds_data.top_holdings
            if th is not None and len(th):
                meta["top_holdings"] = [
                    {"name": str(row.get("Name", idx)), "symbol": str(idx),
                     "pct": round(float(row.get("Holding Percent", 0)) * 100, 2)}
                    for idx, row in th.iterrows()][:10]
        except Exception:
            pass
    except Exception:
        pass
    cache[symbol] = meta
    return meta


# --- weights ---------------------------------------------------------------------------

def holdings_with_weights(holdings: list[dict], level: str) -> list[dict]:
    """Attach a weight_pct to each holding. L2: weight by reconstructed current value;
    L1: weight by amount invested (documented in Methodology). Unpriced/zero rows keep
    weight 0 and are reported, never silently dropped."""
    rows = []
    for h in holdings:
        base = {"name": h["name"], "symbol": h["symbol"], "source": h["source"],
                "type": h["type"], "market": h["market"], "risk": h["risk"],
                "value": 0.0, "priced": False}
        if level == "L2":
            r = agent.reconstruct_holding(h)
            if r.get("status") == "priced":
                base["value"] = r["current_value"]
                base["priced"] = True
        if not base["priced"] and h["total_invested"] > 0:
            base["value"] = h["total_invested"]  # honest fallback: invested amount
            base["priced"] = level != "L2"
        rows.append(base)
    total = sum(r["value"] for r in rows)
    for r in rows:
        r["weight_pct"] = round(r["value"] / total * 100, 2) if total else 0.0
    return rows


def asset_class(row: dict) -> str:
    name = (row["name"] or "").lower()
    typ = (row["type"] or "").lower()
    if "gold" in name or "silver" in name:
        return "Gold & Silver"
    if row["source"] == "crypto" or "crypto" in typ:
        return "Crypto"
    if "mutual fund" in typ:
        return f"MF — {row['market'] or 'Other'}"
    if typ in ("share", "etf"):
        return f"Equity — {row['market'] or 'Other'}"
    return "Other"


def _group_pcts(rows: list[dict], key_fn) -> list[tuple]:
    out: dict[str, float] = {}
    for r in rows:
        out[key_fn(r)] = out.get(key_fn(r), 0.0) + r["weight_pct"]
    return sorted([(k, round(v, 1)) for k, v in out.items() if v > 0],
                  key=lambda kv: -kv[1])


def _cap_bucket(market_cap, currency) -> str:
    if not market_cap:
        return "Unknown"
    if currency == "INR":  # SEBI-flavoured approximations, documented in Methodology
        return ("Large cap" if market_cap >= 1e12 else
                "Mid cap" if market_cap >= 3.3e11 else "Small cap")
    return ("Large cap" if market_cap >= 1e10 else
            "Mid cap" if market_cap >= 2e9 else "Small cap")


# --- sections --------------------------------------------------------------------------

def section_allocation(rows: list[dict], cache: dict) -> dict:
    alloc = _group_pcts(rows, asset_class)
    geo = _group_pcts(rows, lambda r: r["market"] or "Unknown")
    cap: dict[str, float] = {}
    for r in rows:
        if r["source"] in ("yfinance",) and (r["type"] or "").lower() == "share":
            m = fetch_meta(r["symbol"], cache)
            b = _cap_bucket(m["market_cap"], m["currency"])
        elif r["source"] == "crypto":
            b = "Crypto"
        elif r["symbol"]:
            b = "Funds/ETFs (mixed)"
        else:
            b = "Unknown"
        cap[b] = cap.get(b, 0.0) + r["weight_pct"]
    cap_pairs = sorted([(k, round(v, 1)) for k, v in cap.items() if v > 0.5],
                       key=lambda kv: -kv[1])
    return {"asset_classes": alloc, "geography": geo, "market_cap": cap_pairs}


def radar_rows(rows: list[dict]) -> list[tuple]:
    """You-vs-balanced dims. Dims are independent lenses and need not sum to 100."""
    def w(pred):
        return round(sum(r["weight_pct"] for r in rows if pred(r)), 1)
    you = {
        "Equity": w(lambda r: asset_class(r).startswith(("Equity", "MF"))),
        "Debt": w(lambda r: "debt" in (r["type"] or "").lower()
                  or "debt" in (r["name"] or "").lower()),
        "Gold": w(lambda r: asset_class(r) == "Gold & Silver"),
        "International": w(lambda r: (r["market"] or "") in ("US", "Global")),
        "Cash/Alt": w(lambda r: asset_class(r) in ("Crypto", "Other")),
    }
    return [(dim, you[dim], ref) for dim, ref in RADAR_REFERENCE]


def section_diversification(rows: list[dict], overlap_penalty: float) -> dict:
    weights = [r["weight_pct"] for r in rows if r["weight_pct"] > 0]
    hhi = round(metrics.hhi_of_weights(weights), 4)
    eff = round(metrics.effective_holdings(hhi), 1)
    ac_spread = round(metrics.spread([v for _, v in _group_pcts(rows, asset_class)]), 3)
    geo_spread = round(metrics.spread(
        [v for _, v in _group_pcts(rows, lambda r: r["market"] or "Unknown")]), 3)
    score = metrics.diversification_score(eff, ac_spread, geo_spread, overlap_penalty)
    return {
        "holdings_count": len(rows), "hhi": hhi, "effective_holdings": eff,
        "tiny_positions_under_half_pct": sum(1 for r in rows if 0 < r["weight_pct"] < 0.5),
        "asset_class_spread": ac_spread, "geo_spread": geo_spread,
        "overlap_penalty": round(overlap_penalty, 3),
        "diversification_score": score, "radar": radar_rows(rows),
        "score_formula": "40×min(effN/20,1) + 20×asset_spread + 20×geo_spread "
                         "+ 20×(1−overlap_penalty)",
    }


def section_concentration(rows: list[dict], cache: dict, top_n: int = 8) -> dict:
    top = sorted(rows, key=lambda r: r["weight_pct"], reverse=True)[:top_n]
    cum = 0.0
    table = []
    for r in top:
        cum += r["weight_pct"]
        table.append({"name": r["name"], "weight_pct": r["weight_pct"],
                      "cumulative_pct": round(cum, 1)})
    # Look-through for the largest DIRECT stock: direct weight + weight inside owned funds
    # whose disclosed top-10 include it. Degrades to estimate_unavailable.
    lookthrough = None
    directs = [r for r in rows if (r["type"] or "").lower() == "share" and r["weight_pct"] > 0]
    if directs:
        stock = max(directs, key=lambda r: r["weight_pct"])
        fund_positions = []
        for r in rows:
            if r is stock or not r["symbol"] or r["source"] not in ("yfinance",):
                continue
            m = fetch_meta(r["symbol"], cache)
            for th in (m.get("top_holdings") or []):
                root = (stock["symbol"] or "").split(".")[0].lower()
                if root and (root == th["symbol"].split(".")[0].lower()
                             or root in th["name"].lower()):
                    fund_positions.append({"fund": r["name"], "fund_weight_pct":
                                           r["weight_pct"], "stock_pct_in_fund": th["pct"]})
        if fund_positions:
            lt = metrics.lookthrough_exposure(
                stock["weight_pct"],
                [(f["fund_weight_pct"], f["stock_pct_in_fund"]) for f in fund_positions])
            lookthrough = {"stock": stock["name"], **lt, "via": fund_positions}
        else:
            lookthrough = {"stock": stock["name"], "direct_pct": stock["weight_pct"],
                           "note": "estimate unavailable — no owned fund discloses this "
                                   "stock in its top-10"}
    return {"top_holdings": table, "top_n_cumulative_pct": round(cum, 1),
            "max_single_weight_pct": table[0]["weight_pct"] if table else 0,
            "lookthrough": lookthrough}


def section_sector(rows: list[dict], cache: dict) -> dict:
    pairs, covered = [], 0.0
    for r in rows:
        if r["source"] == "yfinance" and (r["type"] or "").lower() == "share" \
                and r["weight_pct"] > 0:
            m = fetch_meta(r["symbol"], cache)
            if m["sector"]:
                pairs.append((m["sector"], r["weight_pct"]))
                covered += r["weight_pct"]
    if not pairs:
        return {"error": "sector data unavailable for the direct-stock sleeve"}
    sectors = metrics.sector_exposure(pairs)
    return {"sectors_pct_of_direct_sleeve": sectors,
            "direct_sleeve_pct_of_portfolio": round(covered, 1),
            "note": "Sectors cover the DIRECT stock sleeve only (funds are mixed); "
                    "weights shown are % of that sleeve, renormalised."}


def section_overlap(rows: list[dict], cache: dict) -> dict:
    funds = []
    for r in rows:
        if r["symbol"] and r["source"] in ("yfinance",) \
                and (r["type"] or "").lower() in ("etf", "mutual fund"):
            m = fetch_meta(r["symbol"], cache)
            if m.get("top_holdings"):
                funds.append((r["name"], [t["name"] for t in m["top_holdings"]]))
    pairs = []
    for i in range(len(funds)):
        for j in range(i + 1, len(funds)):
            ov = round(metrics.fund_overlap(funds[i][1], funds[j][1]) * 100, 0)
            pairs.append({"pair": f"{funds[i][0]} × {funds[j][0]}", "overlap_pct": ov})
    pairs.sort(key=lambda p: -p["overlap_pct"])
    n_funds_total = sum(1 for r in rows
                        if (r["type"] or "").lower() in ("etf", "mutual fund"))
    return {"pairs": pairs[:8],
            "max_overlap_pct": pairs[0]["overlap_pct"] if pairs else 0.0,
            "funds_with_disclosed_holdings": len(funds),
            "funds_total": n_funds_total,
            "note": "Overlap = shared names in disclosed TOP-10 holdings only — true "
                    "overlap is likely higher. Indian MF holdings are not disclosed via "
                    "free APIs; those pairs are estimate-unavailable."}


def _beta_of(symbol: str, market_close) -> float | None:
    try:
        s = yf.Ticker(symbol).history(period="2y")["Close"]
        if s.empty:
            return None
        import pandas as pd
        joined = pd.concat([s.rename("s"), market_close.rename("m")],
                           axis=1, join="inner").dropna()
        rets = joined.pct_change().dropna()
        if len(rets) < 60:
            return None
        return round(metrics.regression_slope(rets["s"].tolist(), rets["m"].tolist()), 2)
    except Exception:
        return None


def section_risk(rows: list[dict], ladder_n: int = 7) -> dict:
    tiers = _group_pcts(rows, lambda r: (r["risk"] or "Unknown").title())
    try:
        market = yf.Ticker(agent.NIFTY50_SYMBOL).history(period="2y")["Close"]
    except Exception:
        market = None
    ladder, weighted, wsum = [], 0.0, 0.0
    if market is not None and not market.empty:
        candidates = [r for r in rows if r["symbol"] and r["source"] in ("yfinance", "crypto")
                      and r["weight_pct"] > 0]
        candidates.sort(key=lambda r: -r["weight_pct"])
        for r in candidates[:ladder_n]:
            b = _beta_of(r["symbol"], market)
            if b is not None:
                ladder.append({"name": r["name"], "beta": b})
                weighted += b * r["weight_pct"]
                wsum += r["weight_pct"]
    port_beta = round(weighted / wsum, 2) if wsum else None
    out = {"risk_tiers_pct": tiers, "beta_ladder": sorted(ladder, key=lambda x: -x["beta"]),
           "portfolio_beta_est": port_beta,
           "beta_coverage_pct_of_portfolio": round(wsum, 1)}
    if port_beta:
        out["modelled_move_if_market_down_15pct"] = round(-15 * port_beta, 1)
    high = next((v for k, v in tiers if k.startswith("High")), 0.0)
    out["high_risk_tier_pct"] = high
    return out


def section_cost(rows: list[dict], cache: dict, total_value: float) -> dict:
    funds = [r for r in rows if r["symbol"]
             and (r["type"] or "").lower() in ("etf", "mutual fund")]
    known, unknown = [], []
    for r in funds:
        er = fetch_meta(r["symbol"], cache)["expense_ratio_pct"] \
            if r["source"] == "yfinance" else None
        if er is not None:
            known.append({"fund": r["name"], "expense_ratio_pct": er,
                          "weight_pct": r["weight_pct"]})
        else:
            unknown.append(r["name"])
    weighted = None
    if known:
        wsum = sum(k["weight_pct"] for k in known)
        if wsum:
            weighted = round(sum(k["expense_ratio_pct"] * k["weight_pct"]
                                 for k in known) / wsum, 2)
    out = {"expense_ratios": sorted(known, key=lambda k: -k["expense_ratio_pct"]),
           "ratio_unavailable_for": unknown,
           "weighted_cost_pct_of_covered": weighted,
           "portfolio_value_for_feedrag": round(total_value, 0),
           "note": "TERs from yfinance where disclosed; Indian MF TERs are not exposed by "
                   "free APIs — marked n/a. Weighted cost covers only funds with a known "
                   "ratio."}
    if weighted and total_value:
        out["fees_10yr_est"] = round(total_value * ((1 + weighted / 100) ** 10 - 1), 0)
    return out


def section_questions(sections: dict) -> dict:
    """Deterministic ranked question agenda from computed findings — questions only, never
    imperatives. The AI layer may rephrase; the numbers and rankings come from here."""
    qs = []
    ov = sections.get("overlap", {})
    if ov.get("pairs"):
        p = ov["pairs"][0]
        if p["overlap_pct"] >= 40:
            qs.append(("HIGH", f"{p['pair']} overlap at {p['overlap_pct']:.0f}% — do both "
                               "need to exist, or is one doing the work of two?", "Overlap"))
    lt = (sections.get("concentration", {}) or {}).get("lookthrough") or {}
    if lt.get("total_pct") and lt.get("via"):
        qs.append(("HIGH", f"Is ~{lt['total_pct']:.0f}% look-through {lt['stock']} "
                           "a conviction you would defend in writing?", "Concentration"))
    div = sections.get("diversification", {})
    radar = {d: y for d, y, _ in div.get("radar", [])}
    if radar.get("Debt", 0) < 1:
        qs.append(("MEDIUM", "Is zero debt a decision about your horizon, or an "
                             "app-default accident?", "Diversification"))
    risk = sections.get("risk", {})
    if risk.get("modelled_move_if_market_down_15pct"):
        qs.append(("MEDIUM", f"A −15% market quarter models to "
                             f"{risk['modelled_move_if_market_down_15pct']}% for you — "
                             "what is your written answer: add, hold, or freeze?", "Risk"))
    tiny = div.get("tiny_positions_under_half_pct", 0)
    if tiny >= 5:
        qs.append(("LOW", f"Which of the {tiny} sub-0.5% positions still earn a slot?",
                   "Diversification"))
    cost = sections.get("cost", {})
    if cost.get("expense_ratios"):
        exp = cost["expense_ratios"][0]
        if exp["expense_ratio_pct"] > 0.9:
            qs.append(("LOW", f"Is {exp['fund']}'s {exp['expense_ratio_pct']}% fee "
                              "justified next to an index alternative?", "Cost"))
    return {"questions": [{"priority": p, "question": q, "from_section": s}
                          for p, q, s in qs]}


# --- top-level assembly -----------------------------------------------------------------

def assemble(portfolio_path: str) -> dict:
    """Read the portfolio, detect the input level, and build every unlocked section's data.
    Each section degrades independently — a fetch failure yields {'error': ...} for that
    section, never a crashed report."""
    holdings, skipped = agent.read_portfolio(portfolio_path)
    level = detect_level(holdings)
    cache = _load_cache()
    rows = holdings_with_weights(holdings, level)
    total_value = sum(r["value"] for r in rows)

    def safe(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"error": f"data unavailable: {e}"}

    sections: dict = {}
    if LEVELS[level] >= 0:
        sections["overlap"] = safe(section_overlap, rows, cache)
    overlap_penalty = min((sections.get("overlap", {}).get("max_overlap_pct") or 0) / 100, 1)
    if LEVELS[level] >= 1:
        sections["allocation"] = safe(section_allocation, rows, cache)
        sections["diversification"] = safe(section_diversification, rows, overlap_penalty)
        sections["concentration"] = safe(section_concentration, rows, cache)
        sections["sector"] = safe(section_sector, rows, cache)
    if LEVELS[level] >= 2:
        sections["risk"] = safe(section_risk, rows)
        sections["cost"] = safe(section_cost, rows, cache, total_value)
    if LEVELS[level] >= 1:
        sections["questions"] = safe(section_questions, sections)

    _save_cache(cache)
    return {"level": level, "total_value": round(total_value, 2),
            "holdings_count": len(rows), "skipped_rows": skipped,
            "rows": rows, "sections": sections}
