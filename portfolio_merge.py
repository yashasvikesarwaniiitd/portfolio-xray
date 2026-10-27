"""Merge an uploaded file into an existing portfolio without double-counting it.

THE PROBLEM. "Add this to my portfolio" has no safe naive answer. Concatenate two
statements and any trade that appears in both - the usual case when someone exports
"last year" and then "this year" and the periods overlap - is counted twice, silently
doubling a position. Replace instead and everything not in the new file is lost. So every
row needs an identity, and a merge is "keep what is there, add what is genuinely new".

TWO IDENTITIES, because the two input formats are different kinds of thing:

  * A STATEMENT row is a trade. Its identity is a hash of ALL its column values (see
    `row_key`), so a trade re-exported in a later file is recognised as the one already
    held. Rows are compared as a multiset rather than a set: two genuinely identical
    fills inside one file are two trades and must both survive, while the same file
    uploaded twice must change nothing. The rule is "keep max(count already held, count
    incoming)" per key, which gives both.

  * A SIP-sheet row is a HOLDING with a column per month. Its identity is the instrument
    (`holding_key`: type, name, symbol, source), not the row's values, because a second
    sheet legitimately carries the same holding with different months filled in. Merging
    extends the existing row. A month present in both keeps the NEW figure and is
    reported as replaced, because a corrected sheet should win over the one it corrects.

PARTS. A portfolio is stored as text. Formats cannot be blended into one table - a SIP
sheet and a trade statement have different columns and mean different things - so a
portfolio is a sequence of self-contained PARTS separated by `PART_DELIM`, each read by the
parser that suits it, with the resulting holdings combined by agent.read_portfolio. That is
what lets one portfolio hold shares from an equity statement and funds from a mutual-fund
statement. A file with no delimiter is a one-part portfolio, so every portfolio saved
before this module existed still reads exactly as it did.

PURE. No network and no clock. Everything here is deterministic and unit-tested.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter

from xray_agent.adapters.broker_import import map_columns

PART_DELIM = "#### xray-part ####"

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

SIP, STATEMENT = "sip", "statement"


# --- cells and keys ------------------------------------------------------------------------

def _norm_header(h) -> str:
    return " ".join(str(h or "").replace("﻿", "").split())


def _norm_cell(v) -> str:
    """Canonical form of one cell, so "1,234.50", "1234.5" and " 1234.50 " are the same
    value and a trade does not look new merely because two exports format it differently."""
    s = " ".join(str(v if v is not None else "").split())
    if not s:
        return ""
    t = s.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    try:
        return repr(round(float(t), 6))
    except ValueError:
        return s.lower()


def row_key(header: list, row: list) -> str:
    """Identity of one row: a hash over ALL its column values.

    Sorted by header NAME, so two exports of the same trade with the columns in a
    different order still match. Empty cells are left out, so a later export that merely
    adds a blank column does not turn every old trade into a new one.
    """
    pairs = sorted((_norm_header(h).lower(), _norm_cell(v))
                   for h, v in zip(header, row) if _norm_cell(v) != "")
    return hashlib.sha1("|".join(f"{h}={v}" for h, v in pairs).encode("utf-8")).hexdigest()[:16]


def holding_key(rec: dict) -> str:
    """Identity of a SIP-sheet holding: the instrument, not its amounts."""
    def g(*names):
        for n in names:
            for k, v in rec.items():
                if _norm_header(k).lower() == n.lower():
                    return " ".join(str(v or "").split()).lower()
        return ""
    return "|".join([g("type"), g("where"), g("symbol"), g("source")])


def _month_key(label: str):
    """(year, month) for a column label like "Mar'25" or "April'25", else None. Keyed on
    the first three letters because the same sheet mixes both spellings."""
    name, _, yy = str(label).strip().partition("'")
    if name.strip()[:3].lower() in _MONTHS and yy.strip().isdigit():
        return 2000 + int(yy.strip()), _MONTHS[name.strip()[:3].lower()]
    return None


def _amount(v) -> float:
    s = str(v if v is not None else "").replace(",", "").replace("₹", "").strip()
    if s in ("", "-", "nan", "NaN", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt(x: float) -> str:
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


# --- parts ---------------------------------------------------------------------------------

def split_parts(text: str) -> list:
    """The parts of a stored portfolio. A file with no delimiter is one part."""
    text = (text or "").replace("\r\n", "\n")
    parts = [p.strip("\n") for p in re.split(rf"(?m)^{re.escape(PART_DELIM)}\s*$", text)]
    return [p for p in parts if p.strip()]


def join_parts(parts: list) -> str:
    return ("\n" + PART_DELIM + "\n").join(p.strip("\n") for p in parts if p.strip()) + "\n"


class Part:
    """One self-contained file inside a portfolio."""

    def __init__(self, kind, header, rows, preamble=None, delimiter=","):
        self.kind = kind
        self.header = header
        self.rows = rows
        self.preamble = preamble or []     # banner rows above a statement's header
        self.delimiter = delimiter

    def render(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=self.delimiter, lineterminator="\n")
        for r in self.preamble:
            w.writerow(r)
        w.writerow(self.header)
        for r in self.rows:
            w.writerow(r)
        return buf.getvalue()

    def header_signature(self) -> tuple:
        return tuple(sorted(_norm_header(h).lower() for h in self.header if _norm_header(h)))


def parse_part(text: str) -> Part:
    """Read one file as a SIP sheet or a trade statement. Raises ValueError if it is neither."""
    rows = [r for r in csv.reader(io.StringIO(text))]
    for i, r in enumerate(rows[:3]):
        names = {_norm_header(c).lower() for c in r}
        if "where" in names and "total invested" in names:
            return Part(SIP, [_norm_header(c) for c in r],
                        [x for x in rows[i + 1:] if any(str(c).strip() for c in x)])
    # A statement: find the header the same way broker_import does - try each delimiter and
    # keep the one whose header row maps the most fields.
    best = None
    for delim in (",", "\t", ";", "|"):
        drows = list(csv.reader(io.StringIO(text), delimiter=delim))
        for i, r in enumerate(drows[:25]):
            mapping, _ = map_columns(r)
            if "date" in mapping and "quantity" in mapping:
                if best is None or len(mapping) > best[0]:
                    best = (len(mapping), delim, drows, i)
                break
    if best is None:
        raise ValueError("That file does not look like a transaction statement or a SIP "
                         "sheet, so it cannot be added to a portfolio.")
    _, delim, drows, i = best
    return Part(STATEMENT, [str(c).replace("﻿", "") for c in drows[i]],
                [x for x in drows[i + 1:] if any(str(c).strip() for c in x)],
                preamble=drows[:i], delimiter=delim)


# --- merging -------------------------------------------------------------------------------

def _merge_statement(have: Part, new: Part) -> dict:
    """Append the incoming trades the part does not already hold."""
    order = {_norm_header(h).lower(): j for j, h in enumerate(new.header)}
    # Re-order incoming cells into the existing column order, so differently-ordered
    # exports of the same statement still dedupe and still append into one rectangle.
    incoming = []
    for r in new.rows:
        cells = []
        for h in have.header:
            j = order.get(_norm_header(h).lower())
            cells.append(r[j] if j is not None and j < len(r) else "")
        incoming.append(cells)
    held = Counter(row_key(have.header, r) for r in have.rows)
    seen: Counter = Counter()
    added = dup = 0
    for r in incoming:
        k = row_key(have.header, r)
        seen[k] += 1
        if seen[k] > held[k]:
            have.rows.append(r)
            added += 1
        else:
            dup += 1
    return {"added": added, "duplicates": dup}


def _merge_sip(have: Part, new: Part) -> dict:
    """Extend the existing holdings with the incoming months, and add holdings not seen."""
    def month_cols(p):
        return [(c, _month_key(c)) for c in p.header if _month_key(c)]

    have_month = {mk: c for c, mk in month_cols(have)}
    # Incoming month label -> the existing column for the same month, or a new column.
    colmap = {}
    for c, mk in month_cols(new):
        if mk in have_month:
            colmap[c] = have_month[mk]
        else:
            have.header.append(c)
            for r in have.rows:
                r.append("")
            have_month[mk] = c
            colmap[c] = c
    hidx = {_norm_header(h).lower(): j for j, h in enumerate(have.header)}
    nidx = {_norm_header(h).lower(): j for j, h in enumerate(new.header)}
    total_j = hidx.get("total invested")

    def rec(part, r):
        return {h: (r[j] if j < len(r) else "") for j, h in enumerate(part.header)}

    # Existing column -> the incoming column that feeds it, so a holding new to this
    # portfolio lands its "April'25" amount under the existing "Apr'25" column.
    from_target = {target: c for c, target in colmap.items()}
    index = {}
    for r in have.rows:
        index.setdefault(holding_key(rec(have, r)), r)

    new_holdings = updated = months_added = months_replaced = months_same = 0
    for r in new.rows:
        rc = rec(new, r)
        name = str(rc.get("Where", "")).strip()
        if not name or name.lower() == "nan":
            continue                              # the sheet's own spacer / summary rows
        key = holding_key(rc)
        mine = index.get(key)
        if mine is None:
            row = [""] * len(have.header)
            for j, h in enumerate(have.header):
                nj = nidx.get(_norm_header(from_target.get(h, h)).lower())
                if nj is not None and nj < len(r):
                    row[j] = r[nj]
            have.rows.append(row)
            index[key] = row
            new_holdings += 1
            continue
        touched = False
        for c, target in colmap.items():
            nj = nidx.get(_norm_header(c).lower())
            tj = hidx.get(_norm_header(target).lower())
            if nj is None or tj is None or nj >= len(r):
                continue
            amt = _amount(r[nj])
            if amt <= 0:
                continue
            old = _amount(mine[tj]) if tj < len(mine) else 0.0
            if abs(old - amt) < 1e-9:
                months_same += 1
                continue
            while len(mine) <= tj:
                mine.append("")
            mine[tj] = _fmt(amt)
            if total_j is not None:
                while len(mine) <= total_j:
                    mine.append("")
                mine[total_j] = _fmt(_amount(mine[total_j]) + amt - old)
            touched = True
            if old > 0:
                months_replaced += 1
            else:
                months_added += 1
        updated += 1 if touched else 0
    return {"new_holdings": new_holdings, "updated": updated, "months_added": months_added,
            "months_replaced": months_replaced, "months_unchanged": months_same}


def merge(existing_text: str, incoming_text: str) -> tuple:
    """(merged_text, report). `existing_text` may be empty, in which case this is just a
    normalised copy of the incoming file. Raises ValueError for an unreadable file."""
    have = [parse_part(p) for p in split_parts(existing_text)]
    report = {"transactions_added": 0, "transactions_duplicate": 0, "holdings_added": 0,
              "holdings_updated": 0, "months_added": 0, "months_replaced": 0,
              "months_unchanged": 0, "sections_added": 0, "notes": []}
    incoming = split_parts(incoming_text)
    if not incoming:
        raise ValueError("The file is empty.")
    for text in incoming:
        new = parse_part(text)
        target = None
        for p in have:
            if p.kind != new.kind:
                continue
            if new.kind == SIP or p.header_signature() == new.header_signature():
                target = p
                break
        if target is None:
            have.append(new)
            report["sections_added"] += 1
            if new.kind == STATEMENT:
                report["transactions_added"] += len(new.rows)
            else:
                report["holdings_added"] += len([r for r in new.rows if r and str(r[0]).strip()])
            if have[:-1]:
                report["notes"].append(
                    "Added as its own section, because its columns differ from anything "
                    "already in this portfolio.")
        elif new.kind == STATEMENT:
            r = _merge_statement(target, new)
            report["transactions_added"] += r["added"]
            report["transactions_duplicate"] += r["duplicates"]
        else:
            r = _merge_sip(target, new)
            report["holdings_added"] += r["new_holdings"]
            report["holdings_updated"] += r["updated"]
            report["months_added"] += r["months_added"]
            report["months_replaced"] += r["months_replaced"]
            report["months_unchanged"] += r["months_unchanged"]
    report["changed"] = bool(
        report["transactions_added"] or report["holdings_added"] or report["months_added"]
        or report["months_replaced"] or report["sections_added"])
    return join_parts([p.render() for p in have]), report


def describe(report: dict) -> str:
    """One plain sentence for the person who just uploaded, saying what actually happened.
    "Added 12 new transactions; 30 were already in this portfolio" tells them the merge
    worked. "Done" would not tell them whether it did anything."""
    bits = []
    if report.get("transactions_added") or report.get("transactions_duplicate"):
        bits.append(f"{report['transactions_added']} new transaction(s) added"
                    + (f", {report['transactions_duplicate']} already present and skipped"
                       if report["transactions_duplicate"] else ""))
    if report.get("holdings_added"):
        bits.append(f"{report['holdings_added']} new holding(s)")
    if report.get("months_added"):
        bits.append(f"{report['months_added']} new month(s) of SIP history")
    if report.get("months_replaced"):
        bits.append(f"{report['months_replaced']} month(s) updated to the newer figure")
    if not report.get("changed"):
        return "Nothing new — everything in that file was already in this portfolio."
    return "; ".join(bits).capitalize() + "."
