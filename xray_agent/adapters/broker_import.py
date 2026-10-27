"""Import a broker order/transaction statement (Groww and friends) into holdings.

Broker exports are not a standard. The same broker renames columns between export
versions, and every broker spells the same six ideas differently. So this does not hardcode
one layout: it normalises the header row and matches each column against a synonym set.
When it cannot find something it needs, it says WHICH columns it saw and WHICH field it
could not fill, because "could not parse your file" is a useless error for a user holding
a file they did not design.

Six fields are needed, and two of them can be derived:

    date      required
    symbol    required (or a name we can label the holding with)
    side      required, unless quantity is signed
    quantity  required
    price     derivable from amount / quantity
    amount    derivable from quantity * price

Everything degrades rather than raises. A row that cannot be understood is reported in
`skipped` with its line number and reason; the rest of the file still imports. One bad row
in a thousand-row statement must not cost the user their whole import.
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime

from ..domain.transactions import BUY, SELL, Transaction

# Column synonyms, matched against the normalised header (lowercased, punctuation stripped,
# whitespace collapsed). Order within a list is preference order.
FIELD_SYNONYMS = {
    "date": ["trade date", "transaction date", "order date", "execution date", "exec date",
             "date time", "datetime", "date", "txn date", "settlement date", "value date"],
    "symbol": ["symbol", "scrip", "scrip code", "ticker", "instrument", "stock symbol",
               "tradingsymbol", "trading symbol", "nse symbol", "bse code", "isin"],
    "isin": ["isin number", "isin code", "isin"],
    "name": ["stock name", "company name", "scheme name", "security name", "instrument name",
             "scrip name", "fund name", "name", "particulars", "description"],
    "side": ["transaction type", "trade type", "order type", "buy sell", "buy or sell",
             "type", "side", "b s", "action", "txn type"],
    "quantity": ["quantity", "qty", "units", "no of shares", "number of shares", "shares",
                 "filled qty", "executed quantity", "no of units"],
    "price": ["trade price", "executed price", "execution price", "average price",
              "avg price", "price per unit", "nav", "rate", "price"],
    "amount": ["net amount", "trade value", "total amount", "gross amount", "turnover",
               "amount", "value", "total"],
    # Not needed to build a transaction, but needed to decide whether it HAPPENED.
    "status": ["order status", "execution status", "trade status", "status"],
    "exchange": ["exchange name", "exchange", "segment"],
}

# A statement lists ORDERS, not trades. Cancelled, rejected and failed orders sit in the
# same file as executed ones, and importing them inflates the portfolio with positions the
# user never held. Groww's export has an "Order status" column for exactly this reason.
EXECUTED_WORDS = {"executed", "execute", "complete", "completed", "success", "successful",
                  "filled", "traded", "confirmed", "settled", "done", "ok"}
NOT_EXECUTED_WORDS = {"cancelled", "canceled", "rejected", "failed", "expired", "pending",
                      "open", "lapsed", "reversed", "cancel", "reject", "aborted"}

BUY_WORDS = {"buy", "b", "bought", "purchase", "purchased", "credit", "cr", "sip",
             "investment", "invest", "subscription"}
SELL_WORDS = {"sell", "s", "sold", "sale", "redeem", "redemption", "redeemed", "debit",
              "dr", "withdrawal", "switch out"}

DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y",
                "%d-%B-%Y", "%d %B %Y", "%Y/%m/%d", "%b %d, %Y", "%d.%m.%Y"]

# A bare NSE ticker needs a suffix before yfinance will recognise it. Applied only when the
# symbol has no suffix already and looks like a ticker rather than a scheme name.
_TICKERISH = re.compile(r"^[A-Z0-9&\-]{1,20}$")


def _norm(header: str) -> str:
    h = str(header or "").replace("﻿", "").strip().lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    return " ".join(h.split())


def map_columns(headers: list) -> tuple:
    """(mapping, unmapped_headers). Longest synonym wins, so 'trade price' beats 'price'."""
    normalised = {_norm(h): h for h in headers if str(h).strip()}
    mapping: dict = {}
    for field, synonyms in FIELD_SYNONYMS.items():
        for syn in sorted(synonyms, key=len, reverse=True):
            for norm_header, original in normalised.items():
                if norm_header == syn and original not in mapping.values():
                    mapping[field] = original
                    break
            if field in mapping:
                break
    # Second pass: allow a containment match for anything still unfilled, so
    # "Buy/Sell Indicator" or "Quantity (Nos.)" are not missed.
    for field, synonyms in FIELD_SYNONYMS.items():
        if field in mapping:
            continue
        for syn in sorted(synonyms, key=len, reverse=True):
            for norm_header, original in normalised.items():
                if syn in norm_header and original not in mapping.values():
                    mapping[field] = original
                    break
            if field in mapping:
                break
    unmapped = [h for h in headers if h and h not in mapping.values()]
    return mapping, unmapped


def parse_date(value) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.split("T")[0].strip()
    # Drop a trailing clock time ("2025-03-14 09:15:00", "14-03-2025 09:15 AM")
    raw = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?$", "", raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value) -> float | None:
    raw = str(value or "").strip()
    if not raw or raw in {"-", "--", "NA", "N/A", "nan", "None"}:
        return None
    neg = raw.startswith("(") and raw.endswith(")")      # (1,234.56) accounting negative
    raw = re.sub(r"[^\d.\-]", "", raw)
    if raw in {"", "-", "."}:
        return None
    try:
        n = float(raw)
    except ValueError:
        return None
    return -n if neg else n


def parse_side(value, quantity: float | None) -> str | None:
    """Explicit side wins; otherwise a signed quantity carries the direction."""
    token = _norm(value)
    if token:
        words = set(token.split())
        if words & BUY_WORDS or token in BUY_WORDS:
            return BUY
        if words & SELL_WORDS or token in SELL_WORDS:
            return SELL
    if quantity is not None and quantity != 0:
        return BUY if quantity > 0 else SELL
    return None


def normalise_symbol(symbol: str, name: str = "", exchange: str = "") -> tuple:
    """(symbol, source). A bare ticker needs a venue suffix before yfinance recognises it,
    and the statement tells us which venue: NSE -> .NS, BSE -> .BO. Both forms resolve for
    alphabetic tickers (verified against live data); a numeric BSE scrip code does not, so
    those are left alone rather than given a suffix that would not work.

    Anything that looks like a fund scheme is left unresolved rather than guessed —
    mapping a scheme name to an AMFI code needs the AMFI list, which is a network call and
    belongs in a tool, not a parser.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None, ""
    if "." in sym or sym.startswith("^") or sym.endswith("-USD"):
        return sym, "crypto" if sym.endswith("-USD") else "yfinance"
    if sym.isdigit() and len(sym) >= 5:          # AMFI scheme codes are numeric
        return sym, "mftool"
    if _TICKERISH.match(sym) and not sym.isdigit():
        suffix = ".BO" if "BSE" in _norm(exchange).upper() else ".NS"
        return f"{sym}{suffix}", "yfinance"
    return sym, ""


_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_VENUE_SUFFIX = re.compile(r"\.(NS|BO)$")
_ETFISH = re.compile(r"(etf|bees|beees|exchange traded)", re.I)


def infer_instrument(symbol: str, source: str, name: str = "", isin: str = "") -> tuple:
    """(type, market) for a holding the statement did not classify.

    A SIP sheet states both in its own columns. A broker statement states neither, so every
    stock imported from one arrived as type "" and market "" - and the engine read that as
    "Other". Concretely: the allocation chart put a book of NSE shares under "Other", the
    sector and direct look-through sections (which only accept type == "share") skipped
    them, and a statement-built portfolio looked smaller and less analysable than the same
    holdings entered by hand. Nothing failed; the sections just quietly had nothing to say.

    Inferred from what the statement DOES give, with no network: the venue suffix and the
    price source. ETF is a name heuristic (BeES, ETF) and is only a heuristic - a wrong
    guess moves a holding between "Share" and "ETF", which changes nothing about its value.
    """
    sym = str(symbol or "").strip().upper()
    if source == "mftool":
        return "Mutual Fund", "India"
    if source == "crypto" or sym.endswith("-USD"):
        return "Crypto", "Global"
    if not sym and not source:
        return "", ""
    # An Indian ISIN says what the instrument is: INE... is a company's equity, INF... is an
    # investment fund - which for something traded on an exchange means an ETF. Groww names
    # ETFs "HDFCAMC - HDFCSML250" or "NIPPONAMC - NETFSILVER", with no "ETF" in them, so the
    # name heuristic alone filed those as ordinary shares and a book's ETFs counted as stocks.
    etf = (str(isin or "").upper().startswith("INF") or bool(_ETFISH.search(name or ""))
           or sym.endswith("BEES.NS"))
    typ = "ETF" if etf else "Share"
    if sym.endswith((".NS", ".BO")):
        return typ, "India"
    if source == "yfinance":
        return typ, "US"
    return "", ""


def order_executed(value) -> bool | None:
    """True / False / None when the status is present but not recognised."""
    token = _norm(value)
    if not token:
        return None
    words = set(token.split())
    if words & NOT_EXECUTED_WORDS:
        return False
    if words & EXECUTED_WORDS:
        return True
    return None


class _FilteredRow(Exception):
    """Row deliberately excluded (e.g. a cancelled order) — not a parse failure."""


class ImportError_(ValueError):
    """Raised only when the file as a whole cannot be understood."""


def read_transactions(text: str) -> tuple:
    """Parse statement text into (transactions, skipped_notes, mapping, unmapped)."""
    if not text.strip():
        raise ImportError_("The file is empty.")

    # csv.Sniffer is handed a sample that STARTS with banner text ("Groww Order
    # Statement", an account line, a blank), finds no consistent delimiter, and falls back
    # to comma - at which point a TAB-separated export parses as one column per line and
    # the header is never found. A real Groww export hit exactly that. So do not ask it:
    # try each delimiter and keep whichever yields a header row mapping the most fields.
    # The parse validates its own delimiter.
    best = None
    for delim in (",", "	", ";", "|"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        for i, row in enumerate(rows[:25]):
            candidate, unmapped_c = map_columns(row)
            if "date" in candidate and "quantity" in candidate:
                if best is None or len(candidate) > len(best[0]):
                    best = (candidate, unmapped_c, rows, i, delim)
                break
    if best is None:
        first = next((r for r in csv.reader(io.StringIO(text))
                      if any(str(c).strip() for c in r)), [])
        seen = [h for h in first if str(h).strip()][:12]
        raise ImportError_(
            "This does not look like a transaction report. A transaction report needs at "
            "least a date column and a quantity column, and this file has neither. "
            f"The columns found were: {', '.join(str(c) for c in seen) or '(none)'}.")

    mapping, unmapped, rows, header_idx, _delimiter = best
    headers = rows[header_idx]
    txns, skipped = [], []
    statuses: dict = {}
    filtered = 0
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(str(c).strip() for c in row):
            continue
        rec = dict(zip(headers, row))
        if "status" in mapping:
            raw = str(rec.get(mapping["status"]) or "").strip() or "(blank)"
            statuses[raw] = statuses.get(raw, 0) + 1
        try:
            txn = _row_to_txn(rec, mapping)
        except _FilteredRow:
            filtered += 1
            continue
        except ValueError as e:
            skipped.append(f"line {n}: {e}")
            continue
        if txn:
            txns.append(txn)
    stats = {"status_counts": statuses, "filtered_not_executed": filtered,
             "status_column": mapping.get("status")}
    if not txns:
        detail = "; ".join(skipped[:5]) if skipped else "every row was empty or unparseable."
        if filtered:
            detail = (f"{filtered} row(s) were excluded because their order status was not "
                      f"executed ({', '.join(sorted(statuses))}).")
        raise ImportError_("No usable transactions found. " + detail)
    return txns, skipped, mapping, unmapped, stats


def _row_to_txn(rec: dict, mapping: dict):
    def val(field):
        col = mapping.get(field)
        return rec.get(col) if col else None

    # An order that never executed is not a trade. Importing cancelled and rejected rows
    # would silently inflate the portfolio with positions the user never held.
    if order_executed(val("status")) is False:
        raise _FilteredRow()

    when = parse_date(val("date"))
    if when is None:
        raise ValueError(f"unreadable date {str(val('date'))[:24]!r}")

    qty = parse_number(val("quantity"))
    price = parse_number(val("price"))
    amount = parse_number(val("amount"))
    side = parse_side(val("side"), qty)

    if qty is None and price and amount:
        qty = amount / price
    if qty is None or qty == 0:
        raise ValueError("no usable quantity")
    if price is None and amount:
        price = abs(amount) / abs(qty)
    if price is None or price <= 0:
        raise ValueError("no usable price (and none derivable from amount / quantity)")
    # When the statement states BOTH the unit price and the amount, the amount is the cash that
    # actually moved and the price is a rounded quote (a mutual-fund NAV is printed to 2dp).
    # units x NAV is therefore a slightly different number from what was paid, and across a
    # long SIP history the rounding accumulates: Rs 89 on a Rs 7.8 lakh book here. Use the
    # amount - but only when the two agree to within 1%, so a row whose "amount" means
    # something else (a fee-inclusive total, a different currency) cannot silently rewrite
    # the price.
    if amount and qty and abs(abs(amount) - abs(qty) * price) <= 0.01 * abs(amount):
        price = abs(amount) / abs(qty)
    if side is None:
        raise ValueError("could not tell buy from sell, and the quantity is unsigned")

    symbol_raw = str(val("symbol") or "").strip()
    name = str(val("name") or "").strip() or symbol_raw
    if not symbol_raw and not name:
        raise ValueError("row identifies no instrument")

    exchange = str(val("exchange") or "")
    symbol, _ = normalise_symbol(symbol_raw or name, name, exchange)
    isin = str(val("isin") or "").strip().upper()
    if not _ISIN.match(isin):
        isin = ""
    return Transaction(date=when, side=side, units=abs(qty), price=abs(price),
                       symbol=symbol or "", name=name or symbol_raw, isin=isin)


def to_holdings(txns: list) -> list:
    """Group transactions into the holding dicts the rest of the engine already consumes.

    The returned shape matches `agent.read_portfolio` — same keys, so every existing tool
    keeps working — plus a `transactions` list. `reconstruct_holding` prefers that when it
    is present, because real trade dates and executed prices beat reconstructing units from
    a monthly amount.
    """
    # ONE POSITION PER INSTRUMENT, not per venue. A stock bought on NSE and again on BSE is one
    # holding in a demat account, but the symbol differs (TCS.NS vs TCS.BO), so grouping on the
    # symbol made two holdings out of it. That doubled the holding count, split the position's
    # P&L and XIRR across two rows, and - because each row was priced on its own exchange's
    # last quote, a day apart - valued the same share at two different prices. The ISIN is the
    # one identifier both venues share, so group on it, falling back to the bare ticker.
    grouped: dict = {}
    for t in txns:
        root = _VENUE_SUFFIX.sub("", t.symbol or "")
        key = t.isin or root or t.name
        grouped.setdefault(key, []).append(t)

    holdings = []
    for key, group in grouped.items():
        group.sort(key=lambda t: t.date)
        # Price on NSE when the instrument has been traded there: it is the deeper market and
        # the one yfinance carries most reliably. BSE only if that is all there is.
        symbols = [t.symbol for t in group if t.symbol]
        nse = [x for x in symbols if x.endswith(".NS")]
        chosen = (nse or symbols or [""])[0]
        symbol, source = normalise_symbol(chosen, group[0].name)
        symbol = chosen or symbol
        isin = next((t.isin for t in group if t.isin), "")
        inferred = infer_instrument(symbol, source, group[0].name, isin)
        gross_buys = sum(t.value for t in group if t.side == BUY)
        holdings.append({
            "name": group[0].name or key,
            "symbol": symbol,
            "source": source,
            "type": inferred[0], "market": inferred[1], "risk": "",
            "total_invested": round(gross_buys, 2),
            # Kept so anything still reading `inflows` degrades instead of breaking; the
            # transactions below are the authoritative record.
            "inflows": [(t.date.year, t.date.month, t.value)
                        for t in group if t.side == BUY],
            "transactions": group,
        })
    holdings.sort(key=lambda h: -h["total_invested"])
    return holdings


def read_broker_csv(path: str, resolve_funds: bool = True) -> tuple:
    """(holdings, notes). Notes carry the column mapping and any skipped rows, so a user
    can see how their file was interpreted rather than having to trust it."""
    if not os.path.exists(path):
        raise ImportError_(f"File not found: {path}")
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    txns, skipped, mapping, unmapped, stats = read_transactions(text)
    holdings = to_holdings(txns)

    # A mutual fund statement identifies a scheme by NAME; every price lookup needs the
    # AMFI code. This is the only network call in the import path, and it degrades to
    # "unresolved" rather than failing the whole file.
    fund_notes: list = []
    if resolve_funds and any(not h["source"] for h in holdings):
        try:
            from .fund_resolver import resolve_holdings
            fund_notes = resolve_holdings(holdings)
        except Exception as e:
            fund_notes = [f"fund-code resolution unavailable ({e}); "
                          "those holdings will be reported as unpriceable"]
    notes = {
        "transactions": len(txns),
        "excluded_not_executed": stats["filtered_not_executed"],
        "order_statuses_seen": stats["status_counts"],
        "holdings": len(holdings),
        "column_mapping": mapping,
        "ignored_columns": unmapped,
        "skipped_rows": skipped,
        "resolved_funds": fund_notes,
        "unresolved_instruments": [h["name"] for h in holdings if not h["source"]],
        "date_range": [min(t.date for t in txns).isoformat(),
                       max(t.date for t in txns).isoformat()],
    }
    return holdings, notes
