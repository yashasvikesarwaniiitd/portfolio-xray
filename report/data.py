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
import fund_holdings
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

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fund_meta_seed.json")


def _load_cache() -> dict:
    """Runtime cache, backed by a committed seed.

    yfinance's fund top-10 holdings are not reachable from the deployed host - the live
    backend reports funds_with_disclosed_holdings: 0 where a local run gets 2 - so fund
    overlap silently had no data in production even though the tool worked. The seed ships
    that metadata for the demo portfolios' symbols, so overlap works on a cold instance
    with no outbound call. It holds public fund data only, never a real holding.
    """
    merged: dict = {}
    for path in (SEED_PATH, CACHE_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                merged.update(json.load(f))
        except Exception:
            continue
    return merged


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
    stale = ent          # kept: a failed refresh should not be worse than an old answer
    meta = {"sector": None, "market_cap": None, "currency": None, "expense_ratio_pct": None,
            "quote_type": None, "top_holdings": None, "ts": date.today().isoformat()}
    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        meta["sector"] = info.get("sector")
        meta["market_cap"] = info.get("marketCap")
        meta["currency"] = info.get("currency")
        meta["quote_type"] = info.get("quoteType")
        meta["long_name"] = info.get("longName")    # lets a ticker be matched to a fund holding by name
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
    # A refresh that came back empty (blocked or rate-limited upstream) must not erase
    # what we already had - stale fund holdings beat none at all.
    if stale and not meta.get("top_holdings") and stale.get("top_holdings"):
        return stale
    cache[symbol] = meta
    return meta


# --- weights ---------------------------------------------------------------------------

def holdings_with_weights(holdings: list[dict], level: str,
                          valued: list[dict] | None = None) -> list[dict]:
    """Attach a weight_pct to each holding. L2: weight by reconstructed current value;
    L1: weight by amount invested (documented in Methodology). Unpriced/zero rows keep
    weight 0 and are reported, never silently dropped.

    `valued` is the result of agent.reconstruct_holding for each holding, in the same order,
    when the caller has already computed it. Valuing a holding means a price download, and
    /analysis valued every holding for the snapshot and then, through here, valued every one
    AGAIN - 51 market-data calls for 25 stocks. Passing the rows it already has costs nothing;
    omitting it keeps the old behaviour for callers that have none.

    That last sentence was not true at L2 until now. The invested-amount fallback below is
    the L1 basis — at L1 there are no prices at all, so invested IS the measure and every
    row uses it. At L2 it was also firing for rows that FAILED to price, which did three
    things the rest of the app does not do:

      * it mixed bases. A basket sat at its cost while everything beside it sat at today's
        value, and the two were compared as if they were the same quantity.
      * it weighted against a total nobody sees. The dashboard headlines "what it's worth
        today" from the snapshot, which excludes unpriced holdings — Rs 52,995 on the
        sample — while these weights divided by Rs 57,996. Every percentage on the page
        was a share of a number shown nowhere.
      * it disagreed with the other path. agent.fetch_concentration feeds the metric
        priced rows only, so the chat said 3.42 effective bets where the dashboard said
        3.6 for the same book, and a reader comparing them had no way to tell which.

    The fallback is now scoped to the level it was written for. An unpriced holding at L2
    keeps weight 0 and is still carried in `rows` with priced=False, so the holdings table
    and the "could not be valued" card report it exactly as before.
    """
    if valued is not None and len(valued) != len(holdings):
        valued = None        # misaligned: recompute rather than weight the wrong holdings
    rows = []
    for i, h in enumerate(holdings):
        base = {"name": h["name"], "symbol": h["symbol"], "source": h["source"],
                "type": h["type"], "market": h["market"], "risk": h["risk"],
                "value": 0.0, "priced": False, "closed": False}
        if level == "L2":
            r = valued[i] if valued is not None else agent.reconstruct_holding(h)
            base["closed"] = r.get("status") == "closed"
            if r.get("status") == "priced":
                base["value"] = r["current_value"]
                base["priced"] = True
        elif h["total_invested"] > 0:
            base["value"] = h["total_invested"]  # L1 basis: invested amount
            base["priced"] = True
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
        # The count the effective-bets figure is OVER, not the count of rows held. They
        # differ whenever something could not be priced, and "4.7 of 6" when only 5 rows
        # entered the maths claims a spread across a holding that was never in it.
        "holdings_count": len(weights),
        "holdings_held": sum(1 for r in rows if not r.get("closed")),
        # A closed (fully sold) holding has no weight because it is gone, not because it
        # could not be priced.
        "unweighted_unpriced": sum(1 for r in rows
                                   if r["weight_pct"] <= 0 and not r.get("closed")),
        "hhi": hhi, "effective_holdings": eff,
        "tiny_positions_under_half_pct": sum(1 for r in rows if 0 < r["weight_pct"] < 0.5),
        "asset_class_spread": ac_spread, "geo_spread": geo_spread,
        "overlap_penalty": round(overlap_penalty, 3),
        "diversification_score": score, "radar": radar_rows(rows),
        "score_formula": "40×min(effN/20,1) + 20×asset_spread + 20×geo_spread "
                         "+ 20×(1−overlap_penalty)",
    }


def section_concentration(rows: list[dict], cache: dict, top_n: int = 8) -> dict:
    # Only holdings that carry weight. A sold or unpriced holding has none, and listing it made
    # "2 holdings hold 100% of your money" about a book with one open position.
    top = sorted((r for r in rows if r["weight_pct"] > 0), key=lambda r: r["weight_pct"],
                 reverse=True)[:top_n]
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


def _fund_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r["type"] or "").lower() in ("etf", "mutual fund")]


def _holdings_source(row: dict, cache: dict) -> dict:
    """What we know about one fund's holdings, and how well.

    Two sources, deliberately ranked. The committed monthly snapshot carries the FULL
    disclosed portfolio with ISINs, which is what SEBI requires every AMC to publish;
    yfinance carries ten names and no ISIN. Where both exist the snapshot wins, because an
    overlap computed from ten names out of two hundred is not a smaller version of the
    right answer, it is a different and much rougher one.
    """
    f = fund_holdings.match(row["name"])
    if f and f.get("status") == "OK":
        pos = fund_holdings.positions(f)
        if pos:
            return {"basis": "full_holdings_isin", "positions": pos, "fund": f,
                    "as_of": f.get("as_of"), "names": fund_holdings.names_by_isin(f)}
        # A fund-of-funds: disclosed, valid, and genuinely opaque at company level.
        return {"basis": "no_look_through", "fund": f, "as_of": f.get("as_of")}
    if row["symbol"] and row["source"] in ("yfinance",):
        m = fetch_meta(row["symbol"], cache)
        if m.get("top_holdings"):
            return {"basis": "top10_names",
                    "names_only": [t["name"] for t in m["top_holdings"]]}
    return {"basis": "none",
            "reason": (f or {}).get("unavailable_reason")
                      or "no disclosed holdings available"}


def section_overlap(rows: list[dict], cache: dict) -> dict:
    """How much of your funds is the same companies twice.

    This used to see Indian mutual funds not at all. The old filter required a yfinance
    symbol, and an Indian MF arrives through AMFI without one, so a book made entirely of
    Indian funds produced an empty overlap table AND a diversification score with a zero
    overlap penalty — the most flattering possible answer, for the least good reason.

    Pairs are now computed on whichever basis both sides support, and each pair says which
    one it used, because the two scales are not comparable. Weighted overlap on full
    holdings ("this % of both funds is literally the same securities") runs well below
    shared-name counts on top-10 lists, and reading one as the other overstates a book's
    diversification or its duplication depending on which way you get it wrong.
    """
    fund_rows = _fund_rows(rows)
    known, opaque, unknown = [], [], []
    for r in fund_rows:
        src = _holdings_source(r, cache)
        if src["basis"] in ("full_holdings_isin", "top10_names"):
            known.append((r, src))
        elif src["basis"] == "no_look_through":
            opaque.append({"fund": r["name"], "as_of": src.get("as_of"),
                           "reason": "fund-of-funds — discloses units of another fund, "
                                     "not companies"})
        else:
            unknown.append({"fund": r["name"], "reason": src.get("reason")})

    pairs = []
    for i in range(len(known)):
        for j in range(i + 1, len(known)):
            (ra, sa), (rb, sb) = known[i], known[j]
            label = f"{ra['name']} × {rb['name']}"
            if sa["basis"] == sb["basis"] == "full_holdings_isin":
                res = metrics.fund_overlap_by_isin(sa["positions"], sb["positions"])
                shared = [sa["names"].get(k) or sb["names"].get(k)
                          for k in res["common_isins"][:5]]
                pairs.append({"pair": label, "overlap_pct": res["overlap_pct"],
                              "count_overlap_pct": res["count_overlap_pct"],
                              "common_count": res["common_count"],
                              "shared_top": [n for n in shared if n],
                              "basis": "full_holdings_isin",
                              "as_of": sa.get("as_of") or sb.get("as_of")})
            else:
                # One side has only ten names, so the pair can only be answered on ten
                # names. Comparing a full list against a top-10 on NAMES would score the
                # fully disclosed fund down purely for disclosing more.
                a = sa.get("names_only") or list((sa.get("names") or {}).values())[:10]
                b = sb.get("names_only") or list((sb.get("names") or {}).values())[:10]
                if not (a and b):
                    continue
                pairs.append({"pair": label, "basis": "top10_names",
                              "overlap_pct": round(metrics.fund_overlap(a, b) * 100, 0)})
    pairs.sort(key=lambda p: -p["overlap_pct"])

    full = sum(1 for _, s in known if s["basis"] == "full_holdings_isin")
    cov = fund_holdings.coverage()
    as_of = sorted({p["as_of"] for p in pairs if p.get("as_of")})
    note = ""
    if full:
        note += ("Overlap = the share of two funds that is the SAME securities, joined on "
                 "ISIN from each AMC's full monthly SEBI disclosure "
                 f"(as of {', '.join(as_of)}). ")
    if any(p["basis"] == "top10_names" for p in pairs):
        note += ("Pairs marked top10_names fall back to shared names in disclosed TOP-10 "
                 "holdings; that number runs much higher than a weighted one and is not "
                 "comparable with it. ")
    if not pairs:
        note += ("No pair of your funds has disclosed holdings available, so overlap is "
                 "estimate-unavailable for this book. ")
    note += (f"The holdings snapshot covers {cov['funds_available']} of "
             f"{cov['funds_total']} funds; a fund missing from it is not shown as zero "
             "overlap, it is not shown at all.")
    return {"pairs": pairs[:8],
            "max_overlap_pct": pairs[0]["overlap_pct"] if pairs else 0.0,
            "max_overlap_basis": pairs[0]["basis"] if pairs else None,
            "funds_with_full_holdings": full,
            "funds_with_disclosed_holdings": len(known),
            "funds_total": len(fund_rows),
            "opaque_funds": opaque,
            "funds_without_holdings": unknown,
            "as_of": as_of,
            "note": note}


_DEBT_WORDS = ("debt", "liquid", "gilt", "bond", "overnight", "money market", "income",
               "treasury", "credit risk", "duration", "floater", "corporate bond")

# What each kind of disclosed instrument is, for the class summary. Equity-like kinds are
# handled separately because they become per-company rows rather than one aggregate.
_KIND_CLASS = {"DEBT": "Debt", "CASH": "Cash & equivalents",
               "DERIVATIVE": "Hedges (short futures)", "OTHER": "Other"}


def _plain_class(row: dict) -> str:
    """The asset class of a holding we cannot see inside. Name-based, like asset_class()."""
    name = (row["name"] or "").lower()
    typ = (row["type"] or "").lower()
    if "gold" in name or "silver" in name:
        return "Gold & Silver"
    if row["source"] == "crypto" or "crypto" in typ:
        return "Crypto"
    if typ == "share":
        return "Equity"
    if any(w in name for w in _DEBT_WORDS) or "debt" in typ:
        return "Debt"
    if typ == "etf":
        return "ETF (not looked through)"
    if typ == "mutual fund":
        return "Fund (holdings not disclosed)"
    return "Other"


def section_lookthrough(rows: list[dict], cache: dict, top_n: int = 15,
                        keep: int = 400) -> dict:
    """What the portfolio is really made of, counting every route to the same thing.

    One line per underlying thing, with every FORM it is held in. A company can be held
    directly, inside a mutual fund, inside two funds, or all three; the useful number is the
    total, and the useful detail is the split. So HDFC Bank held as a share AND inside a fund
    is one row that says "Direct 1.9% + via Parag Parikh 1.9% = 3.8%", rather than two rows
    in two sections that a reader has to add up by eye.

    Across asset classes, not just stocks. Funds in the snapshot are opened up: their equity
    holdings become company rows, and their debt, cash and hedge positions become class rows,
    so a "flexi cap" that is 30% arbitrage and T-bills shows as exactly that. Everything else
    is one line carrying its own class - a gold ETF is Gold & Silver, a debt fund is Debt,
    a crypto pair is Crypto - so the rows always add up to the whole portfolio.

    What it will NOT do is pretend. A fund with no disclosure is one line labelled as
    undisclosed rather than being treated as holding nothing, and the share of the book that
    was actually opened up is reported alongside. The joins are conservative (see
    fund_holdings.resolve_isin): an unmatched direct stock stays its own row. ETFs are not
    opened up yet - they appear as a single line.
    """
    index = fund_holdings.company_index()
    out: dict = {}          # key -> exposure row

    def add(key, name, isin, klass, amount, form, via=None, pct_in_fund=None):
        e = out.setdefault(key, {"key": key, "name": name, "isin": isin,
                                 "asset_class": klass, "total_pct": 0.0,
                                 "direct_pct": 0.0, "via_funds_pct": 0.0, "forms": []})
        e["total_pct"] += amount
        if form == "Direct":
            e["direct_pct"] += amount
        elif form == "Via fund":
            e["via_funds_pct"] += amount
        # Any other form is a line that stands for itself (an undisclosed fund, an ETF).
        e["forms"].append({"form": form, "via": via,
                           "pct_of_portfolio": round(amount, 3),
                           "pct_in_fund": None if pct_in_fund is None
                           else round(pct_in_fund, 2)})

    seen_funds, direct_pct, looked_through_pct, as_of = [], 0.0, 0.0, set()
    fund_total = 0
    for r in rows:
        w = r["weight_pct"]
        if w <= 0:
            continue
        typ = (r["type"] or "").lower()
        is_fund = typ in ("etf", "mutual fund")
        if is_fund:
            fund_total += 1

        f = fund_holdings.match(r["name"]) if typ == "mutual fund" else None
        if f and f.get("status") == "OK":
            seen_funds.append(r["name"])
            looked_through_pct += w
            as_of.add(f.get("as_of"))
            for h in f.get("holdings") or []:
                pct = float(h.get("weight_pct") or 0.0)
                if not pct:
                    continue
                amount = w * pct / 100.0
                kind = h.get("kind")
                if kind in fund_holdings.EQUITY_KINDS and h.get("isin"):
                    add(h["isin"], h.get("name") or h["isin"], h["isin"], "Equity", amount,
                        "Via fund", via=r["name"], pct_in_fund=pct)
                elif kind == "MF_UNITS" and h.get("isin"):
                    add(h["isin"], h.get("name") or h["isin"], h["isin"],
                        "Fund units", amount, "Via fund", via=r["name"], pct_in_fund=pct)
                else:
                    klass = _KIND_CLASS.get(kind, "Other")
                    add("class:" + klass, {"Debt": "Debt & money-market instruments",
                                           "Cash & equivalents": "Cash, TREPS & receivables",
                                           "Hedges (short futures)": "Short futures (hedges)",
                                           "Other": "Other instruments"}[klass],
                        None, klass, amount, "Via fund", via=r["name"], pct_in_fund=pct)
            continue

        if typ == "share" and r["source"] == "yfinance":
            names = [r["name"]]
            meta = fetch_meta(r["symbol"], cache) if r["symbol"] else {}
            if meta.get("long_name"):
                names.insert(0, meta["long_name"])
            isin = fund_holdings.resolve_isin(names, index)
            key = isin or "name:" + fund_holdings.norm_company(r["name"])
            add(key, r["name"], isin, "Equity", w, "Direct")
            direct_pct += w
            continue

        # Anything else is one line carrying its own class.
        klass = _plain_class(r)
        add("holding:" + (r["symbol"] or r["name"]), r["name"], None, klass, w,
            "Direct" if not is_fund else "Fund", pct_in_fund=None)

    if not out:
        return {"error": "nothing in this portfolio could be priced"}

    ranked = sorted(out.values(), key=lambda e: -e["total_pct"])
    # Keep every row the reader holds directly or in more than one form (those are the
    # answers to "where do I hold X"), then fill with the largest of the rest.
    must = [e for e in ranked if e["direct_pct"] > 0 or len(e["forms"]) > 1]
    rest = [e for e in ranked if e not in must]
    shown = sorted(must + rest, key=lambda e: -e["total_pct"])[:keep]
    for e in shown:
        for k in ("total_pct", "direct_pct", "via_funds_pct"):
            e[k] = round(e[k], 3)
        e["held_via"] = len(e["forms"])
        e["forms"].sort(key=lambda x: -x["pct_of_portfolio"])

    by_class: dict = {}
    for e in out.values():
        by_class[e["asset_class"]] = by_class.get(e["asset_class"], 0.0) + e["total_pct"]
    total_listed = sum(e["total_pct"] for e in out.values())
    companies = [e for e in out.values() if e["asset_class"] == "Equity"]
    return {
        "exposures": shown,
        "top_exposures": [e for e in shown if e["asset_class"] == "Equity"][:top_n],
        "by_class": [{"asset_class": k, "pct": round(v, 2)}
                     for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])],
        "distinct_companies": len(companies),
        "rows_total": len(out),
        "funds_seen_through": seen_funds,
        "funds_total": fund_total,
        "portfolio_pct_covered": round(direct_pct + looked_through_pct, 1),
        "looked_through_pct": round(looked_through_pct, 1),
        "direct_pct": round(direct_pct, 1),
        "sum_of_rows_pct": round(total_listed, 2),
        "as_of": sorted(a for a in as_of if a),
        "note": ("Each line is one company or asset, with every form you hold it in. "
                 f"{len(seen_funds)} of {fund_total} funds are opened up from their full "
                 f"SEBI monthly disclosure ({round(looked_through_pct, 1)}% of the "
                 "portfolio); a fund without one, and any ETF, is a single line and is "
                 "not looked through. Direct stocks are matched to fund holdings by "
                 "company name, and an unmatched one stays its own line."),
    }


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
        # Two bases, two thresholds. 40% of ten disclosed NAMES is common and mild; 25% of
        # a fund's VALUE being the same securities as another fund's is not. One threshold
        # across both would either never fire on full holdings or fire constantly on
        # top-10.
        trigger = 25 if p["basis"] == "full_holdings_isin" else 40
        if p["overlap_pct"] >= trigger:
            qs.append(("HIGH", f"{p['pair']} overlap at {p['overlap_pct']:.0f}% — do both "
                               "need to exist, or is one doing the work of two?", "Overlap"))
    lt = sections.get("lookthrough") or {}
    # The largest company reached by more than one route - held directly AND inside a
    # fund, or inside two funds. A single route is just a position; several is a decision
    # that was probably made more than once without anyone adding it up.
    for e in (lt.get("top_exposures") or []):
        if e["held_via"] > 1 and e["total_pct"] >= 3:
            how = ("directly and through " + str(e["held_via"] - 1) + " fund(s)"
                   if e["direct_pct"] > 0 else f"through {e['held_via']} different funds")
            qs.append(("MEDIUM", f"{e['name']} reaches {e['total_pct']:.1f}% of the "
                                 f"portfolio {how} — was that one decision or several?",
                       "Look-through"))
            break
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
        sections["lookthrough"] = safe(section_lookthrough, rows, cache)
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
