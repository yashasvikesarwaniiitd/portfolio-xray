"""Tests for the frozen fund-holdings snapshot.

Two halves. The first uses hand-built funds to pin the rules — what the gate rejects, what
counts as overlap, how names are matched — because those are the decisions, and asserting
them against the real file would only prove the real file has not changed. The second half
reads the committed snapshot, because a data file is shipped code: if someone replaces it
with a half-parsed one, that is a production bug and a test should catch it.
"""
import json

import pytest

import fund_holdings as F
import metrics


def fund(**kw):
    base = {"input_name": "Some Fund Direct Growth", "status": "OK",
            "as_of": "2026-09-30", "source_url": "https://amc.example/p.xlsx",
            "holdings": [{"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY",
                          "weight_pct": 100.0}]}
    base.update(kw)
    return base


# --- the gate ------------------------------------------------------------------------------

def test_a_clean_fund_passes():
    assert F.problems_with(fund()) is None


def test_a_partial_sheet_is_refused():
    """The failure this is really guarding against. A parser that stops at the first blank
    row returns a fund with twelve holdings and a confident-looking overlap number, and
    nothing downstream can tell that from a fund that genuinely holds twelve things. The
    weights summing to 100 is the only independent check the disclosure gives us."""
    half = fund(holdings=[{"isin": "INE002A01018", "name": "Reliance",
                           "kind": "EQUITY", "weight_pct": 41.0}])
    why = F.problems_with(half)
    assert why and "partial" in why


def test_a_misread_column_is_refused():
    bad = fund(holdings=[{"isin": "Reliance Industries", "name": "x",
                          "kind": "EQUITY", "weight_pct": 100.0}])
    assert "malformed ISIN" in F.problems_with(bad)


def test_a_sheet_read_twice_is_refused():
    dup = fund(holdings=[{"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY",
                          "weight_pct": 50.0},
                         {"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY",
                          "weight_pct": 50.0}])
    assert "twice" in F.problems_with(dup)


def test_an_unsourced_fund_is_refused():
    assert "source URL" in F.problems_with(fund(source_url=None))
    assert "as-of" in F.problems_with(fund(as_of=None))


def test_rows_with_no_isin_are_allowed_because_treps_has_none():
    """TREPS, repo and net receivables legitimately carry no ISIN. Rejecting them would
    reject every real fund in the file."""
    ok = fund(holdings=[{"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY",
                         "weight_pct": 96.0},
                        {"isin": None, "name": "TREPS", "kind": "CASH",
                         "weight_pct": 4.0}])
    assert F.problems_with(ok) is None


def test_hedges_below_the_grand_total_do_not_trip_the_sum_check():
    """A real case from the snapshot: an arbitrage fund's rows sum to 100 above the grand
    total, and its short futures sit below it, pulling the total to ~98.5. That is a
    correctly parsed sheet, not a partial one."""
    arb = fund(holdings=[{"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY",
                          "weight_pct": 100.0},
                         {"isin": None, "name": "Reliance Oct Future",
                          "kind": "DERIVATIVE", "weight_pct": -1.45}])
    assert F.problems_with(arb) is None


def test_a_failing_fund_is_downgraded_not_dropped(tmp_path):
    """It must still appear, carrying why. A fund that silently vanishes looks identical
    to a fund the user does not hold."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema_version": 1, "funds": [
        fund(input_name="Broken Fund", holdings=[
            {"isin": "INE002A01018", "name": "x", "kind": "EQUITY", "weight_pct": 12.0}])]}),
        encoding="utf-8")
    doc = F.load(str(p))
    f = doc["funds"][0]
    assert f["status"] == "UNAVAILABLE"
    assert f["holdings"] == []
    assert "rejected by validation" in f["unavailable_reason"]


def test_a_missing_or_broken_file_degrades_to_empty(tmp_path):
    """Same posture as storage.py: losing this feature must not break a request."""
    assert F.load(str(tmp_path / "absent.json"))["funds"] == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert F.load(str(bad))["funds"] == []


# --- what counts as overlap ----------------------------------------------------------------

def test_only_claims_on_a_company_count():
    """Two funds both parking cash in TREPS are not overlapping, and a fund holding
    another fund's units is a look-through question, not an overlap one."""
    f = fund(holdings=[
        {"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY", "weight_pct": 50.0},
        {"isin": "US02079K3059", "name": "Alphabet", "kind": "FOREIGN_EQUITY",
         "weight_pct": 20.0},
        {"isin": "INE041025011", "name": "Embassy REIT", "kind": "REIT_INVIT",
         "weight_pct": 10.0},
        {"isin": "INE879O01068", "name": "Some Liquid Fund", "kind": "MF_UNITS",
         "weight_pct": 10.0},
        {"isin": "IN0020230085", "name": "T-Bill", "kind": "DEBT", "weight_pct": 6.0},
        {"isin": None, "name": "TREPS", "kind": "CASH", "weight_pct": 4.0}])
    assert F.positions(f) == {"INE002A01018": 50.0, "US02079K3059": 20.0,
                              "INE041025011": 10.0}


def test_a_company_listed_twice_is_one_position():
    f = fund(holdings=[
        {"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY", "weight_pct": 4.0},
        {"isin": "INE002A01018", "name": "Reliance", "kind": "EQUITY", "weight_pct": 2.0}])
    assert F.positions(f) == {"INE002A01018": 6.0}


# --- the metric ----------------------------------------------------------------------------

def test_weighted_overlap_is_the_shared_value_not_the_shared_names():
    """The two numbers answer different questions and are easy to confuse. These funds
    share one name out of two — 50% by count — but only 5% of their value, because the
    shared company is tiny in one of them."""
    a = {"INE002A01018": 5.0, "INE009A01021": 95.0}
    b = {"INE002A01018": 60.0, "INE467B01029": 40.0}
    r = metrics.fund_overlap_by_isin(a, b)
    assert r["overlap_pct"] == 5.0
    assert r["count_overlap_pct"] == 50.0
    assert r["common_isins"] == ["INE002A01018"]


def test_identical_funds_overlap_completely():
    a = {"INE002A01018": 60.0, "INE009A01021": 40.0}
    assert metrics.fund_overlap_by_isin(a, dict(a))["overlap_pct"] == 100.0


def test_disjoint_funds_overlap_not_at_all():
    r = metrics.fund_overlap_by_isin({"INE002A01018": 100.0}, {"INE009A01021": 100.0})
    assert r["overlap_pct"] == 0.0 and r["common_count"] == 0


def test_overlap_is_symmetric():
    a, b = {"INE002A01018": 30.0, "INE009A01021": 70.0}, {"INE002A01018": 80.0}
    assert (metrics.fund_overlap_by_isin(a, b)["overlap_pct"]
            == metrics.fund_overlap_by_isin(b, a)["overlap_pct"])


def test_an_empty_side_raises_rather_than_returning_zero():
    """Zero overlap and "no data" are different answers; conflating them would report a
    fund we know nothing about as safely uncorrelated."""
    with pytest.raises(ValueError):
        metrics.fund_overlap_by_isin({}, {"INE002A01018": 100.0})


# --- matching a portfolio row to a fund ----------------------------------------------------

def test_the_plan_is_ignored_because_the_portfolio_is_disclosed_per_scheme():
    """Direct and Regular are one portfolio at two expense ratios. A Regular holder must
    still get holdings — refusing to match would be wrong, not cautious."""
    doc = {"funds": [fund(input_name="Parag Parikh Flexi Cap Fund Direct Growth")]}
    for name in ["Parag Parikh Flexi Cap Fund - Direct Growth",
                 "Parag Parikh Flexi Cap Fund Regular Growth",
                 "PARAG PARIKH FLEXI CAP FUND - DIRECT PLAN - GROWTH OPTION"]:
        assert F.match(name, doc) is not None, name


def test_the_same_fund_tokenised_two_ways_still_matches():
    """AMFI says "Kotak Small Cap Fund"; the AMC's own sheet says "Kotak Smallcap Fund"."""
    doc = {"funds": [fund(input_name="Kotak Small Cap Fund Direct Growth")]}
    assert F.match("Kotak Smallcap Fund Direct Growth", doc) is not None


def test_a_fund_we_do_not_have_matches_nothing():
    doc = {"funds": [fund(input_name="Kotak Small Cap Fund Direct Growth")]}
    assert F.match("Nippon India Growth Fund Direct Growth", doc) is None
    assert F.match("", doc) is None


# --- the committed snapshot ------------------------------------------------------------------

def test_the_shipped_snapshot_parses_and_every_ok_fund_survives_the_gate():
    """A data file is shipped code. If a later snapshot is half-parsed, this fails here
    rather than in a user's overlap table."""
    doc = F.load()
    assert doc["schema_version"] == 1, "unreadable or missing snapshot"
    assert len(doc["funds"]) == 10
    for f in doc["funds"]:
        if f["status"] == "OK":
            assert F.problems_with(f) is None, f["input_name"]
            # A fund-of-funds holds units, not companies, so an empty equity sleeve is
            # correct for it and only for it.
            if f["look_through_available"]:
                assert F.positions(f), f["input_name"] + " has no equity-like holdings"
        else:
            assert f["unavailable_reason"], f["input_name"] + " is unavailable with no reason"
            assert f["holdings"] == []


def test_the_shipped_snapshot_reports_its_own_coverage_honestly():
    c = F.coverage()
    assert c["funds_total"] == 10
    assert c["funds_available"] == 7, "coverage changed — update this and the README"
    assert c["as_of"] == ["2026-09-30"]
    assert len(c["unavailable"]) == 3


def test_the_same_company_spelled_two_ways_is_one_position():
    """The reason the join key is an ISIN. Two AMCs write the same company differently,
    and a name join would report them as two separate holdings and zero overlap."""
    jio = F.positions(F.match("JioBlackRock Flexi Cap Fund Direct Growth"))
    tata = F.positions(F.match("Tata Digital India Fund Direct Growth"))
    infy = "INE009A01021"
    assert infy in jio and infy in tata
    names = {F.names_by_isin(F.match(n))[infy]
             for n in ["JioBlackRock Flexi Cap Fund Direct Growth",
                       "Tata Digital India Fund Direct Growth"]}
    assert len(names) > 1, "pick a company the two funds spell differently"


def test_the_fund_of_funds_is_marked_as_having_no_look_through():
    """Its disclosure is one line: units of an overseas fund. Reporting zero overlap for
    it would be a claim we cannot support; reporting that we cannot see through it is."""
    fof = F.match("Axis Global Equity Alpha FoF Direct Growth")
    assert fof["status"] == "OK"
    assert fof["look_through_available"] is False
    assert F.positions(fof) == {}, "a FoF holds fund units, not companies"


# --- the report sections it feeds ------------------------------------------------------------

def mf_rows(*pairs):
    return [{"name": n, "symbol": None, "source": "amfi", "type": "Mutual Fund",
             "market": "India", "weight_pct": w, "risk": None} for n, w in pairs]


def test_an_all_indian_book_now_gets_overlap_at_all():
    """The regression this whole module exists for. section_overlap required a yfinance
    symbol, and an Indian MF has none, so a book like this produced an empty pairs list
    and a diversification score with a ZERO overlap penalty — the most flattering answer
    available, for the worst reason."""
    from report import data
    ov = data.section_overlap(mf_rows(
        ("Parag Parikh Flexi Cap Fund - Direct Growth", 50.0),
        ("JioBlackRock Flexi Cap Fund Direct Growth", 50.0)), {})
    assert ov["pairs"], "two fully disclosed Indian funds must produce a pair"
    p = ov["pairs"][0]
    assert p["basis"] == "full_holdings_isin"
    assert p["overlap_pct"] > 0 and p["common_count"] > 0
    assert p["as_of"] == "2026-09-30"
    assert ov["max_overlap_pct"] > 0, "the diversification penalty reads this"


def test_a_fund_we_cannot_see_is_reported_not_scored_as_zero():
    """Kotak blocks automated downloads. An unknown fund must never be quietly treated as
    overlapping with nothing, which would raise the diversification score for a reason
    that is purely an artefact of our data collection."""
    from report import data
    ov = data.section_overlap(mf_rows(
        ("Parag Parikh Flexi Cap Fund - Direct Growth", 50.0),
        ("Kotak Small Cap Fund Direct Growth", 50.0)), {})
    assert [u["fund"] for u in ov["funds_without_holdings"]] == [
        "Kotak Small Cap Fund Direct Growth"]
    assert ov["funds_total"] == 2 and ov["funds_with_full_holdings"] == 1
    assert not ov["pairs"], "one side has no holdings, so there is no pair to score"
    assert "not shown at all" in ov["note"]


def test_the_fund_of_funds_is_listed_as_opaque_rather_than_dropped():
    from report import data
    ov = data.section_overlap(mf_rows(
        ("Parag Parikh Flexi Cap Fund - Direct Growth", 50.0),
        ("Axis Global Equity Alpha FoF Direct Growth", 50.0)), {})
    assert [o["fund"] for o in ov["opaque_funds"]] == [
        "Axis Global Equity Alpha FoF Direct Growth"]


def test_look_through_counts_one_company_across_several_funds():
    from report import data
    rows = mf_rows(("Parag Parikh Flexi Cap Fund - Direct Growth", 40.0),
                   ("JioBlackRock Flexi Cap Fund Direct Growth", 40.0),
                   ("Tata Digital India Fund Direct Growth", 20.0))
    lt = data.section_lookthrough(rows, {})
    assert lt["portfolio_pct_covered"] == 100.0
    top = lt["top_exposures"][0]
    assert top["held_via"] >= 2, "the headline exposure should be one reached twice"
    # The identity: a company's portfolio weight is the sum of (fund weight x its weight
    # inside that fund), and nothing else.
    expected = sum(v["contributes_pct"] for v in top["via"])
    assert abs(top["total_pct"] - expected) < 0.02
    for v in top["via"]:
        assert abs(v["contributes_pct"]
                   - dict((r["name"], r["weight_pct"]) for r in rows)[v["fund"]]
                   * v["pct_in_fund"] / 100.0) < 0.02


def test_look_through_says_how_much_of_the_book_it_could_not_see():
    """Half this portfolio is a fund with no disclosure. Reporting exposures without
    saying so would present a half-view as a whole one."""
    from report import data
    lt = data.section_lookthrough(mf_rows(
        ("Parag Parikh Flexi Cap Fund - Direct Growth", 50.0),
        ("Kotak Small Cap Fund Direct Growth", 50.0)), {})
    assert lt["portfolio_pct_covered"] == 50.0
    assert lt["funds_seen_through"] == ["Parag Parikh Flexi Cap Fund - Direct Growth"]
    assert "1 of 2 funds" in lt["note"]


def test_look_through_degrades_when_nothing_is_disclosed():
    from report import data
    lt = data.section_lookthrough(mf_rows(("Kotak Small Cap Fund Direct Growth", 100.0)), {})
    assert "error" in lt
