"""Portfolio X-Ray — Streamlit frontend, built to the approved Claude Design.

Three surfaces behind a dark rail: Ask the analyst · Portfolio analysis · Enter holdings.
Design tokens (warm paper #FAF7F2, ink #241F1C, rust #8C2F27, olive #7E9B45, gold #B8862B;
Space Grotesk / Source Serif 4 / IBM Plex Mono) come straight from the design.

Every figure shown is computed by the backend in Python — this file only formats. Where the
design proposed a metric we cannot honestly compute (market-cap look-through inside funds,
a fee "category average"), it is replaced by the nearest real one and labelled; see README.
The Anthropic key lives only on the backend. Run: uv run streamlit run app.py
"""
import html as _html
import io
import json
import os
import re
import time

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Portfolio X-Ray", page_icon="🩻", layout="wide",
                   initial_sidebar_state="expanded")

# ---------------------------------------------------------------- backend plumbing

def _backend_url() -> str:
    # st.secrets RAISES when no secrets.toml exists anywhere (fresh clone / local dev),
    # so the env fallback must live behind a try, not a .get() default.
    try:
        return st.secrets["BACKEND_URL"]
    except Exception:
        return os.environ.get("BACKEND_URL", "http://localhost:8000")


BACKEND = _backend_url().rstrip("/")
COLD_NOTE = ("Waking the free-tier server (~30s) — it sleeps after 15 idle minutes "
             "to stay free. Worth the wait.")


def call(method: str, path: str, cold_slot=None, **kw):
    """HTTP call with cold-start UX: if the first byte takes >3s, say the free tier is
    waking up rather than showing a bare spinner."""
    t0 = time.time()
    kw.setdefault("timeout", 240)
    try:
        r = requests.request(method, f"{BACKEND}{path}", **kw)
        if time.time() - t0 > 3 and cold_slot is not None:
            cold_slot.caption(COLD_NOTE)
        return r.status_code, r.content, r.headers
    except requests.exceptions.RequestException as e:
        return 0, str(e).encode(), {}


def call_json(method: str, path: str, cold_slot=None, **kw):
    code, content, _ = call(method, path, cold_slot, **kw)
    try:
        return code, json.loads(content)
    except Exception:
        return code, {"error": content.decode(errors="replace")[:300]}


# ---------------------------------------------------------------- formatting helpers

def inr(n, decimals: int = 0) -> str:
    """Indian digit grouping: ₹18,42,367 (last three, then pairs) — as in the design."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    whole = f"{abs(n):.{decimals}f}"
    frac = ""
    if "." in whole:
        whole, frac = whole.split(".")
        frac = "." + frac
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return f"{sign}₹{whole}{frac}"


def pct(n, decimals: int = 1, signed: bool = False) -> str:
    if n is None:
        return "—"
    return f"{n:+.{decimals}f}%" if signed else f"{n:.{decimals}f}%"


def md_to_html(text: str) -> str:
    """Minimal markdown → HTML so assistant prose can live inside the design's serif
    block: escapes first, then **bold**, `code`, bullet lists and paragraphs."""
    out, buf, lines = [], [], (text or "").split("\n")

    def flush_para():
        if buf:
            out.append("<p>" + " ".join(buf) + "</p>")
            buf.clear()

    def inline(s: str) -> str:
        s = _html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    in_list = False
    for raw in lines:
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if bullet:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(bullet.group(1)) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            flush_para()
        elif heading:
            flush_para()
            out.append("<p><strong>" + inline(heading.group(1)) + "</strong></p>")
        else:
            buf.append(inline(line))
    if in_list:
        out.append("</ul>")
    flush_para()
    return "".join(out)


# ---------------------------------------------------------------- design system (CSS)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Mono:wght@500;600&display=swap');

html, body, [class*="css"], .stApp, button, input, textarea, select {
  font-family: 'Space Grotesk', sans-serif !important;
}
.stApp { background: #FAF7F2; }
#MainMenu, footer, header [data-testid="stStatusWidget"] { visibility: hidden; }
.block-container { padding: 1.6rem 2.2rem 3rem; max-width: 1240px; }
.mono { font-family: 'IBM Plex Mono', monospace; }

/* ---------- dark rail ---------- */
[data-testid="stSidebar"] > div { background: #241F1C; padding-top: 1.1rem; }
[data-testid="stSidebar"] * { color: #C3B6A8; }
[data-testid="stSidebar"] .stButton > button {
  width: 100%; text-align: left; justify-content: flex-start;
  border-radius: 8px; font-size: 13.5px; font-weight: 500; padding: 9px 12px;
  background: transparent; color: #C3B6A8; border: none;
}
[data-testid="stSidebar"] .stButton > button:hover { background: #312923; color: #FFFFFF; }
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background: #8C2F27; color: #FFFFFF; font-weight: 600;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover { background: #A83B31; }
.rail-brand { font-size: 16px; font-weight: 600; color: #FFFFFF; letter-spacing: -.2px; }
.rail-sub { font-size: 12px; color: #9C8E80; margin-top: 2px; }
.rail-label { font-size: 10.5px; font-weight: 600; letter-spacing: 1.4px;
  text-transform: uppercase; color: #B5A695; margin: 4px 0 10px; }
.rail-value { font-family: 'IBM Plex Mono', monospace; font-size: 21px; color: #FFFFFF; }
.rail-delta { font-size: 12px; margin-top: 2px; }
.rail-hr { height: 1px; background: #3E352E; margin: 18px 0; }
.rail-card { border: 1px solid #3E352E; border-radius: 10px; background: #312923;
  padding: 13px 14px; }
.rail-card .t { font-size: 13px; font-weight: 600; color: #FFFFFF; }
.rail-card .b { font-size: 12px; color: #A29486; line-height: 1.5; margin-top: 5px; }
.rail-foot { font-size: 11px; color: #B5A695; line-height: 1.6; margin-top: 16px; }

/* ---------- page header ---------- */
.page-h { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 20px; border-bottom: 1px solid #EDE6DC; padding-bottom: 16px; margin-bottom: 22px; }
.page-h h1 { font-size: 20px; font-weight: 600; letter-spacing: -.2px; margin: 0;
  color: #241F1C; }
.page-h .s { font-size: 13px; color: #8A8078; margin-top: 3px; }
.pill { display: inline-flex; align-items: center; gap: 7px; background: #F6EAE7;
  border-radius: 999px; padding: 6px 12px; font-size: 11.5px; color: #8C2F27;
  font-weight: 500; white-space: nowrap; }
.pill .dot { width: 6px; height: 6px; border-radius: 50%; background: #8C2F27; }

/* ---------- cards ---------- */
.card { background: #FFFFFF; border: 1px solid #E8E0D6; border-radius: 14px;
  padding: 20px 22px; }
.card-dark { background: #241F1C; border-radius: 14px; padding: 20px 22px; color: #FFF; }
.k-label { font-size: 12px; color: #8A8078; }
.card-dark .k-label { color: #A99B8C; }
.k-big { font-family: 'IBM Plex Mono', monospace; font-size: 30px; font-weight: 600;
  color: #FFFFFF; line-height: 1.15; margin-top: 4px; }
.k-num { font-family: 'IBM Plex Mono', monospace; font-size: 26px; color: #241F1C;
  line-height: 1.15; margin-top: 4px; }
.k-num .u { font-size: 14px; color: #8A8078; }
.k-note { font-size: 12px; margin-top: 5px; }
.k-foot { font-size: 11px; color: #948A80; margin-top: 10px; }
.up { color: #7E9B45; } .warn { color: #B8862B; } .down { color: #8C2F27; }
.muted { color: #8A8078; } .ink { color: #241F1C; }
.sec-t { font-size: 14.5px; font-weight: 600; color: #241F1C; }
.sec-s { font-size: 11.5px; color: #948A80; }

/* ---------- bars ---------- */
.spark { display: flex; gap: 3px; align-items: flex-end; }
.spark div { flex: 1; border-radius: 2px; min-height: 2px; }
.stack { display: flex; height: 12px; border-radius: 6px; overflow: hidden; gap: 2px;
  margin: 14px 0 16px; }
.legend { display: flex; align-items: center; gap: 10px; font-size: 13px;
  padding: 5px 0; color: #241F1C; }
.legend .sw { width: 9px; height: 9px; border-radius: 3px; flex-shrink: 0; }
.legend .n { flex: 1; }
.legend .v { font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; }
.track { height: 7px; border-radius: 4px; background: #F0EAE1; overflow: hidden; }
.track > div { height: 7px; border-radius: 4px; background: #8C2F27; }

/* ---------- insight cards ---------- */
.ins { background: #FFFFFF; border: 1px solid #E8E0D6; border-radius: 10px;
  padding: 14px 16px; margin-bottom: 10px; }
.ins.high { border-left: 3px solid #8C2F27; }
.ins.medium { border-left: 3px solid #B8862B; }
.ins.low { border-left: 3px solid #5F7C2C; }
.ins .row { display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; margin-bottom: 4px; }
.ins .t { font-size: 13.5px; font-weight: 600; color: #241F1C; }
.ins .v { font-family: 'IBM Plex Mono', monospace; font-size: 13px; }
.ins .b { font-size: 13px; color: #6E655C; line-height: 1.6; }

/* ---------- holdings table ---------- */
.tbl { background: #FFFFFF; border: 1px solid #E8E0D6; border-radius: 12px;
  overflow: hidden; }
.tbl .hd, .tbl .tr { display: grid;
  grid-template-columns: 2.6fr 1.4fr 2fr 1.2fr 1.2fr; gap: 16px; padding: 12px 20px;
  align-items: center; }
.tbl .hd { background: #FAF6F0; font-size: 11px; letter-spacing: .5px;
  text-transform: uppercase; color: #948A80; }
.tbl .tr { border-top: 1px solid #F0EAE1; font-size: 13.5px; color: #241F1C; }
.tbl .r { text-align: right; font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; }
.tbl .ty { color: #8A8078; font-size: 12.5px; }
.tbl .share { display: flex; align-items: center; gap: 10px; }

/* ---------- chat ---------- */
.msg-user { align-self: flex-end; max-width: 560px; background: #241F1C; color: #FFFFFF;
  border-radius: 14px 14px 4px 14px; padding: 13px 17px; font-size: 14px;
  line-height: 1.55; margin: 0 0 22px auto; width: fit-content; }
.msg-a { display: flex; gap: 14px; max-width: 820px; margin-bottom: 24px; }
.msg-a .av { width: 30px; height: 30px; flex-shrink: 0; border-radius: 50%;
  background: #241F1C; color: #D9A441; font-size: 13px; font-weight: 600;
  display: flex; align-items: center; justify-content: center; }
.prose { font-family: 'Source Serif 4', serif; font-size: 16px; line-height: 1.65;
  color: #3B342E; min-width: 0; }
.prose p { margin: 0 0 10px; }
.prose ul { margin: 4px 0 10px; padding-left: 20px; }
.prose li { margin-bottom: 5px; }
.prose code { font-family: 'IBM Plex Mono', monospace; font-size: 13.5px;
  background: #F3EDE4; padding: 1px 5px; border-radius: 4px; }
.tag { display: inline-block; background: #F3EDE4; border-radius: 999px;
  padding: 5px 11px; font-size: 11.5px; color: #5C534B; margin: 2px 4px 2px 0;
  font-family: 'IBM Plex Mono', monospace; }
.refused { background: #F6EAE7; border-left: 3px solid #8C2F27; border-radius: 8px;
  padding: 10px 14px; font-size: 12.5px; color: #8C2F27; max-width: 820px;
  margin: -12px 0 24px 44px; }

/* ---------- main-area buttons / inputs ---------- */
.block-container .stButton > button {
  border-radius: 999px; border: 1px solid #DED5C9; background: #FFFFFF;
  color: #5C534B; font-size: 12.5px; font-weight: 500; padding: 7px 14px;
}
.block-container .stButton > button:hover { border-color: #8C2F27; color: #8C2F27; }
.block-container .stButton > button[kind="primary"] {
  background: #241F1C; color: #FFFFFF; border: none; border-radius: 9px;
  font-weight: 600; padding: 10px 20px;
}
.block-container .stButton > button[kind="primary"]:hover { background: #8C2F27; }
.stDownloadButton > button { border-radius: 8px; border: 1px solid #DED5C9;
  background: #FFFFFF; color: #5C534B; font-size: 12.5px; }
.stDownloadButton > button:hover { border-color: #8C2F27; color: #8C2F27; }
[data-testid="stChatInput"] textarea { font-family: 'Space Grotesk', sans-serif; }
div[data-testid="stExpander"] details { border: 1px solid #E8E0D6; border-radius: 12px;
  background: #FFFFFF; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

INK, RUST, OLIVE, GOLD, MUTED = "#241F1C", "#8C2F27", "#7E9B45", "#B8862B", "#C6BBAC"
STACK_COLORS = [INK, RUST, "#C08A5E", "#DDBE9A", MUTED, "#9C8E80", "#E8E0D6"]


# ---------------------------------------------------------------- components

def sparkline(series: list, height: int = 40, dark: bool = True) -> str:
    """Bars of real month-end portfolio value. Bar heights are relative to the window's
    max; the final bar turns olive when the portfolio is above where it started."""
    vals = [s["value"] for s in series if s.get("value")]
    if len(vals) < 2:
        return ""
    top = max(vals) or 1
    base = "#3E352E" if dark else "#E8E0D6"
    bars = []
    for i, v in enumerate(vals):
        h = max(6, round(v / top * 100))
        last = i == len(vals) - 1
        color = (OLIVE if vals[-1] >= vals[0] else RUST) if last else (
            RUST if v >= vals[0] else base)
        bars.append(f'<div style="height:{h}%;background:{color}"></div>')
    return f'<div class="spark" style="height:{height}px">{"".join(bars)}</div>'


def stacked(pairs: list) -> str:
    total = sum(v for _, v in pairs) or 1
    seg = "".join(f'<div style="width:{v / total * 100:.2f}%;'
                  f'background:{STACK_COLORS[i % len(STACK_COLORS)]}"></div>'
                  for i, (_, v) in enumerate(pairs))
    rows = "".join(
        f'<div class="legend"><span class="sw" style="background:'
        f'{STACK_COLORS[i % len(STACK_COLORS)]}"></span>'
        f'<span class="n">{_html.escape(str(n))}</span>'
        f'<span class="v">{v:.1f}%</span></div>'
        for i, (n, v) in enumerate(pairs))
    return f'<div class="stack">{seg}</div>{rows}'


def insight(title: str, value: str, body: str, tone: str = "medium") -> str:
    tone_cls = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(tone, tone)
    vcolor = {"high": RUST, "medium": GOLD, "low": "#5F7C2C"}.get(tone_cls, GOLD)
    return (f'<div class="ins {tone_cls}"><div class="row">'
            f'<div class="t">{_html.escape(title)}</div>'
            f'<div class="v" style="color:{vcolor}">{_html.escape(value)}</div></div>'
            f'<div class="b">{_html.escape(body)}</div></div>')


def page_header(title: str, sub: str, pill: str | None = None) -> None:
    p = (f'<div class="pill"><span class="dot"></span>{_html.escape(pill)}</div>'
         if pill else "")
    st.markdown(f'<div class="page-h"><div><h1>{_html.escape(title)}</h1>'
                f'<div class="s">{_html.escape(sub)}</div></div>{p}</div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------- state

ss = st.session_state
ss.setdefault("page", "ask")
ss.setdefault("analysis", None)
ss.setdefault("source_label", None)
ss.setdefault("csv_text", None)      # user's own portfolio, held in memory only
ss.setdefault("chat", [])
ss.setdefault("queued", None)
ss.setdefault("report", None)
ss.setdefault("rows", pd.DataFrame([
    {"Holding name": "", "Symbol / AMFI code": "", "Type": "Share", "Market": "India",
     "Risk": "Med", "Amount invested ₹": None} for _ in range(4)]))

TYPES = ["Share", "ETF", "Mutual Fund", "Crypto", "Basket"]
MARKETS = ["India", "US", "Global"]
RISKS = ["Low", "Med", "High"]


def load_analysis(csv_text: str | None, label: str) -> None:
    slot = st.empty()
    with st.spinner("Reconstructing units from your SIP history, then valuing at today's "
                    "prices…"):
        if csv_text:
            code, data = call_json("POST", "/analysis", cold_slot=slot,
                                   files={"file": ("portfolio.csv", csv_text.encode(),
                                                   "text/csv")})
        else:
            code, data = call_json("GET", "/analysis", cold_slot=slot)
    slot.empty()
    if code == 200 and "snapshot" in data:
        ss.analysis, ss.source_label, ss.csv_text = data, label, csv_text
        ss.report = None
        ss.page = "analysis"
    else:
        st.error(data.get("error", "The backend is unreachable — give the free tier ~30s "
                                   "and try again."))


def rows_to_csv(df: pd.DataFrame) -> tuple[str, int]:
    """Turn the entry grid into our real CSV schema. `source` is INFERRED (numeric code →
    mftool, crypto type → crypto, any other symbol → yfinance, no symbol → manual) so the
    user never has to know about data providers."""
    header = ("Mode,App,Type,Market,Risk,Where,symbol,source,"
              "Estimated Returns (3Y),Total Invested\n")
    lines, n = [], 0
    for _, r in df.iterrows():
        name = str(r.get("Holding name") or "").strip()
        amount = r.get("Amount invested ₹")
        if not name or amount in (None, "") or pd.isna(amount):
            continue
        sym = str(r.get("Symbol / AMFI code") or "").strip()
        typ = str(r.get("Type") or "Share").strip()
        source = ("mftool" if sym.isdigit() else "crypto" if typ == "Crypto"
                  else "yfinance" if sym else "manual")
        safe_name = name.replace(",", " ")
        lines.append(f"Equity,Manual,{typ},{r.get('Market') or 'India'},"
                     f"{r.get('Risk') or 'Med'},{safe_name},{sym},{source},-,"
                     f"{float(amount):.0f}")
        n += 1
    return header + "\n".join(lines) + "\n", n


# ---------------------------------------------------------------- the dark rail

with st.sidebar:
    st.markdown('<div class="rail-brand">Portfolio X-Ray</div>'
                '<div class="rail-sub">A clearer look at what you own</div>',
                unsafe_allow_html=True)
    st.write("")
    nav = [("ask", "Ask the analyst"), ("analysis", "Portfolio analysis"),
           ("holdings", "Enter my holdings")]
    for key, label in nav:
        if st.button(label, key=f"nav_{key}", use_container_width=True,
                     type="primary" if ss.page == key else "secondary"):
            ss.page = key
            st.rerun()

    st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
    st.markdown('<div class="rail-label">At a glance</div>', unsafe_allow_html=True)

    if ss.analysis:
        snap = ss.analysis["snapshot"]
        cls = "up" if (snap.get("total_pnl_abs") or 0) >= 0 else "down"
        arrow = "▲" if (snap.get("total_pnl_abs") or 0) >= 0 else "▼"
        st.markdown(
            f'<div class="rail-value">{inr(snap["total_current_value"])}</div>'
            f'<div class="rail-delta {cls}">{arrow} {inr(abs(snap["total_pnl_abs"]))} · '
            f'{pct(abs(snap["total_pnl_pct"]))}</div>', unsafe_allow_html=True)
        tl = ss.analysis.get("timeline") or []
        if tl:
            st.markdown(sparkline(tl) +
                        f'<div style="font-size:11px;color:#7E8B9B;margin-top:6px">'
                        f'Last {len(tl)} months, reconstructed</div>',
                        unsafe_allow_html=True)
        st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="rail-card"><div class="t">{_html.escape(ss.source_label or "")}'
            f'</div><div class="b">{ss.analysis["holdings_priced"]} of '
            f'{ss.analysis["holdings_total"]} holdings priced · input tier '
            f'{ss.analysis["level"]}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="rail-card"><div class="t">Nothing loaded yet</div>'
                    '<div class="b">Start with the sample portfolio — one click, no '
                    'typing.</div></div>', unsafe_allow_html=True)
        if st.button("▶ Load the sample", key="rail_sample", use_container_width=True,
                     type="primary"):
            load_analysis(None, "Sample portfolio")
            st.rerun()

    st.markdown('<div class="rail-foot">Educational only — not advice, not '
                'SEBI-registered. Nothing you upload is stored.</div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------- page: ask

def page_ask() -> None:
    n = ss.analysis["holdings_total"] if ss.analysis else None
    page_header("Ask the analyst",
                "Plain questions about your portfolio. Plain answers, with the numbers "
                "behind them.",
                f"Reading your {n} holdings" if n else "No portfolio loaded yet")

    if not ss.chat:
        st.markdown(
            '<div class="msg-a"><div class="av">X</div><div class="prose">'
            '<p>Ask me what you actually own. I compute every number in Python — '
            'concentration, exact XIRR from your SIP dates, fund overlap, beta — and I '
            'explain what it means.</p><p>I will not tell you what to buy or sell, and I '
            'will not predict prices. That boundary is enforced in code, not just '
            'promised: try the third suggestion below.</p></div></div>',
            unsafe_allow_html=True)

    for role, text, meta in ss.chat:
        if role == "user":
            st.markdown(f'<div class="msg-user">{_html.escape(text)}</div>',
                        unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="msg-a"><div class="av">X</div>'
                        f'<div class="prose">{md_to_html(text)}</div></div>',
                        unsafe_allow_html=True)
            if meta.get("refused"):
                st.markdown(
                    '<div class="refused">That refusal is the product working, not '
                    'failing: the router classifies advice-seeking questions and answers '
                    'them from a fixed script before any tool runs. 100% refusal '
                    'correctness on 52 labelled cases — see the README eval table.</div>',
                    unsafe_allow_html=True)
            elif meta.get("tools"):
                tags = "".join(f'<span class="tag">{_html.escape(t)}</span>'
                               for t in meta["tools"])
                st.markdown(f'<div style="margin:-14px 0 24px 44px">'
                            f'<span style="font-size:11.5px;color:#948A80">Python tools '
                            f'used:</span> {tags}</div>', unsafe_allow_html=True)

    st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
    chips = ["How concentrated am I?", "Do I own the same thing twice?",
             "Should I sell my worst fund?", "What is XIRR?"]
    cols = st.columns(len(chips))
    for col, chip in zip(cols, chips):
        if col.button(chip, key=f"chip_{chip}", use_container_width=True):
            ss.queued = chip
            st.rerun()

    typed = st.chat_input("Ask about value, XIRR, beta, overlap, NAVs, news…")
    question = ss.queued or typed
    ss.queued = None
    if question:
        history = [{"role": r, "content": t} for r, t, _ in ss.chat]
        slot = st.empty()
        with st.spinner("Routing → Python tools → answer…"):
            code, data = call_json("POST", "/chat", cold_slot=slot,
                                   json={"message": question, "history": history})
        slot.empty()
        answer = data.get("answer", "The backend is unreachable — give the free tier "
                                    "~30s and retry.")
        ss.chat.append(("user", question, {}))
        ss.chat.append(("assistant", answer,
                        {"refused": data.get("refused"),
                         "tools": data.get("tools_used") or []}))
        st.rerun()


# ---------------------------------------------------------------- page: analysis

def page_analysis() -> None:
    if not ss.analysis:
        page_header("Portfolio analysis", "Nothing loaded yet.")
        st.markdown('<div class="card"><div class="sec-t">Load a portfolio to see this'
                    '</div><div class="b" style="font-size:13px;color:#6E655C;'
                    'margin-top:6px">The sample is six real instruments with a synthetic '
                    'SIP history — enough to show concentration, overlap and exact XIRR.'
                    '</div></div>', unsafe_allow_html=True)
        st.write("")
        c1, c2 = st.columns([1, 3])
        if c1.button("▶ Load the sample", type="primary", use_container_width=True):
            load_analysis(None, "Sample portfolio")
            st.rerun()
        if c2.button("Enter my own holdings", use_container_width=True):
            ss.page = "holdings"
            st.rerun()
        return

    a = ss.analysis
    snap, sec = a["snapshot"], a["sections"]
    div = sec.get("diversification", {})
    conc = sec.get("concentration", {})
    ov = sec.get("overlap", {})
    cost = sec.get("cost", {})
    priced = [h for h in snap["holdings"] if h.get("status") == "priced"]
    as_of = priced[0].get("price_date") if priced else "—"

    page_header("Portfolio analysis",
                f"{a['holdings_total']} holdings · prices as of {as_of} · "
                f"input tier {a['level']}",
                ss.source_label)

    # --- KPI row: value+sparkline · XIRR vs market · effective bets · fees -----------
    k = st.columns([1.5, 1, 1, 1])
    with k[0]:
        pnl_cls = "up" if snap["total_pnl_abs"] >= 0 else "down"
        arrow = "▲" if snap["total_pnl_abs"] >= 0 else "▼"
        st.markdown(
            f'<div class="card-dark"><div class="k-label">What it\'s worth today</div>'
            f'<div class="k-big">{inr(snap["total_current_value"])}</div>'
            f'<div class="k-note {pnl_cls}">{arrow} {inr(abs(snap["total_pnl_abs"]))} '
            f'against {inr(snap["total_invested"])} invested</div>'
            f'<div style="margin-top:10px">'
            f'{sparkline(a.get("timeline") or [], height=30)}</div></div>',
            unsafe_allow_html=True)
    with k[1]:
        mk = a.get("market") or {}
        xirr = snap.get("portfolio_xirr_pct")
        ref = mk.get("annualised_pct")
        bar_you = min(max((xirr or 0) / 25 * 100, 2), 100)
        bar_mk = min(max((ref or 0) / 25 * 100, 2), 100)
        st.markdown(
            f'<div class="card"><div class="k-label">Your return a year (XIRR)</div>'
            f'<div class="k-num">{pct(xirr)}</div>'
            f'<div class="k-note muted">NIFTY 50 did {pct(ref)} over the same window</div>'
            f'<div style="margin-top:12px"><div class="track" style="margin-bottom:5px">'
            f'<div style="width:{bar_you:.0f}%"></div></div>'
            f'<div class="track"><div style="width:{bar_mk:.0f}%;background:#C6BBAC">'
            f'</div></div></div>'
            f'<div class="k-foot">Yours is money-weighted (your SIP timing); the index '
            f'line is point-to-point.</div></div>', unsafe_allow_html=True)
    with k[2]:
        effn = div.get("effective_holdings")
        total_h = div.get("holdings_count") or a["holdings_total"]
        filled = int(round(effn or 0))
        pips = "".join(
            f'<div style="flex:1;height:6px;border-radius:2px;background:'
            f'{RUST if i < filled else "#E8E0D6"}"></div>' for i in range(total_h))
        st.markdown(
            f'<div class="card"><div class="k-label">How spread out you are</div>'
            f'<div class="k-num">{effn if effn is not None else "—"} '
            f'<span class="u">of {total_h}</span></div>'
            f'<div class="k-note warn">They behave like '
            f'{effn if effn is not None else "—"} independent bets</div>'
            f'<div style="display:flex;gap:3px;margin-top:12px">{pips}</div>'
            f'<div class="k-foot">1 ÷ HHI. Overlapping funds and big positions shrink '
            f'this below your holding count.</div></div>', unsafe_allow_html=True)
    with k[3]:
        wc = cost.get("weighted_cost_pct_of_covered")
        covered = len(cost.get("expense_ratios") or [])
        unknown = len(cost.get("ratio_unavailable_for") or [])
        tenyr = cost.get("fees_10yr_est")
        if wc:
            body = (f'<div class="k-num">{wc:.2f}%</div>'
                    f'<div class="k-note muted">≈ {inr(tenyr)} over ten years at this '
                    f'rate</div>'
                    f'<div class="k-foot">Covers {covered} fund(s) with a disclosed TER'
                    + (f'; {unknown} not disclosed on free APIs' if unknown else "")
                    + '</div>')
        else:
            body = ('<div class="k-num muted">n/a</div>'
                    '<div class="k-note muted">No fund here discloses its expense ratio '
                    'via free APIs</div>'
                    '<div class="k-foot">Indian mutual-fund TERs are not machine-readable '
                    'without paid data.</div>')
        st.markdown(f'<div class="card"><div class="k-label">Yearly fees you pay</div>'
                    f'{body}</div>', unsafe_allow_html=True)

    st.write("")

    # --- allocation + worth-a-look --------------------------------------------------
    left, right = st.columns([1, 1])
    with left:
        alloc = sec.get("allocation", {})
        classes = [(str(n), float(v)) for n, v in (alloc.get("asset_classes") or [])]
        geo = [(str(n), float(v)) for n, v in (alloc.get("geography") or [])]
        st.markdown(
            '<div class="card"><div style="display:flex;align-items:baseline;'
            'justify-content:space-between"><div class="sec-t">Where your money sits'
            '</div><div class="sec-s">by asset class</div></div>'
            + stacked(classes) +
            '<div style="height:1px;background:#F0EAE1;margin:16px 0"></div>'
            '<div style="display:flex;align-items:baseline;justify-content:space-between">'
            '<div class="sec-t">Which market you\'re exposed to</div>'
            '<div class="sec-s">by geography</div></div>'
            + stacked(geo) +
            '<div class="k-foot">Funds are shown as one line each — free data does not '
            'reveal the individual stocks inside Indian mutual funds, so no market-cap '
            'look-through is claimed here.</div></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="sec-t" style="margin-bottom:12px">Worth a look</div>',
                    unsafe_allow_html=True)
        cards = []
        if ov.get("pairs"):
            p = ov["pairs"][0]
            tone = "HIGH" if p["overlap_pct"] >= 40 else "MEDIUM"
            cards.append(insight(
                "Two funds own much the same thing", f'{p["overlap_pct"]:.0f}%',
                f'{p["pair"]} share that share of their disclosed top-10 holdings. '
                f'Two fee lines, largely one bet. (Top-10 basis, so the true figure is '
                f'likely higher.)', tone))
        if conc.get("top_holdings"):
            top = conc["top_holdings"]
            k4 = min(4, len(top))
            cum = top[k4 - 1]["cumulative_pct"]
            cards.append(insight(
                f"{k4} holdings hold {cum:.0f}% of your money", f'{cum:.0f}%',
                f'Largest single position is {top[0]["name"]} at '
                f'{top[0]["weight_pct"]:.1f}%. The rest barely move the needle either way.',
                "HIGH" if cum >= 55 else "MEDIUM"))
        if snap["unpriced_invested"]:
            cards.append(insight(
                "Some holdings can't be valued", inr(snap["unpriced_invested"]),
                f'{snap["total_count"] - snap["priced_count"]} holding(s) have no '
                f'reconstructable price — a manual basket, or listed after your SIP dates. '
                f'They are excluded from every total rather than guessed.', "MEDIUM"))
        score = div.get("diversification_score")
        if score is not None:
            cards.append(insight(
                "Diversification score", f"{score:.0f}/100",
                f'Formula on the report\'s Methodology sheet: effective bets, asset-class '
                f'spread, geography spread, and a penalty for fund overlap '
                f'({div.get("overlap_penalty", 0) * 100:.0f}%).',
                "LOW" if score >= 60 else "MEDIUM"))
        for q in (sec.get("questions", {}).get("questions") or [])[:2]:
            cards.append(insight(f'Question — {q["from_section"]}', q["priority"],
                                 q["question"], q["priority"]))
        st.markdown("".join(cards) or
                    insight("Nothing flagged", "—",
                            "Every computed finding sits inside the comfort lines this "
                            "report uses.", "LOW"), unsafe_allow_html=True)

    st.write("")

    # --- holdings table -------------------------------------------------------------
    st.markdown('<div style="display:flex;align-items:baseline;justify-content:'
                'space-between;margin-bottom:12px"><div class="sec-t">Your holdings</div>'
                f'<div class="sec-s">{len(priced)} priced of {a["holdings_total"]}'
                '</div></div>', unsafe_allow_html=True)
    total_val = snap["total_current_value"] or 1
    rows_html = ['<div class="tbl"><div class="hd"><div>Holding</div><div>Type</div>'
                 '<div>Share of money</div><div class="r">XIRR / yr</div>'
                 '<div class="r">Value</div></div>']
    for h in sorted(priced, key=lambda x: -(x.get("current_value") or 0)):
        w = (h.get("current_value") or 0) / total_val * 100
        xirr = h.get("xirr_pct")
        xcls = "up" if (xirr or 0) >= 0 else "down"
        rows_html.append(
            f'<div class="tr"><div>{_html.escape(h["name"])}</div>'
            f'<div class="ty">{_html.escape(h.get("type") or "—")}</div>'
            f'<div class="share"><div class="track" style="flex:1">'
            f'<div style="width:{min(w * 3, 100):.0f}%"></div></div>'
            f'<span class="v mono" style="width:46px;text-align:right">{w:.1f}%</span>'
            f'</div>'
            f'<div class="r {xcls}">{pct(xirr) if xirr is not None else "—"}</div>'
            f'<div class="r">{inr(h.get("current_value"))}</div></div>')
    unavailable = [h for h in snap["holdings"] if h.get("status") != "priced"]
    for h in unavailable:
        rows_html.append(
            f'<div class="tr"><div>{_html.escape(h["name"])}</div>'
            f'<div class="ty">{_html.escape(h.get("type") or "—")}</div>'
            f'<div class="share" style="font-size:12px;color:#948A80">'
            f'{_html.escape((h.get("reason") or "not priced")[:58])}</div>'
            f'<div class="r muted">—</div>'
            f'<div class="r muted">{inr(h.get("invested"))} in</div></div>')
    rows_html.append("</div>")
    st.markdown("".join(rows_html), unsafe_allow_html=True)

    st.write("")
    b1, b2, _ = st.columns([1.1, 1, 2])
    with b1:
        if st.button("📊 Generate health report", type="primary",
                     use_container_width=True):
            prog = st.status("Building your report…", expanded=True)
            prog.write("Computing metrics (exact units, HHI, overlap, betas)…")
            prog.write("Rendering 11 charts…")
            files = ({"file": ("portfolio.csv", ss.csv_text.encode(), "text/csv")}
                     if ss.csv_text else None)
            code, content, headers = call("POST", "/report", files=files)
            if code == 200:
                prog.write("Writing guarded AI insights…")
                prog.update(label="Report ready", state="complete")
                ss.report = (content, dict(headers))
            else:
                prog.update(label="Report failed", state="error")
                try:
                    st.error(json.loads(content).get("answer")
                             or json.loads(content).get("error"))
                except Exception:
                    st.error("Report generation failed — try again shortly.")
    with b2:
        if st.button("Ask about this", use_container_width=True):
            ss.page = "ask"
            st.rerun()
    if ss.report:
        content, headers = ss.report
        st.download_button("⬇ Download Portfolio_Health_Report.xlsx", data=content,
                           file_name="Portfolio_Health_Report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")
        st.caption(f"Input tier {headers.get('X-Report-Level', '?')} · AI insight cost "
                   f"₹{headers.get('X-Report-Cost-INR', '?')} · every insight is "
                   f"checked: no buy/sell language, and every number must exist in the "
                   f"computed section data.")


# ---------------------------------------------------------------- page: holdings

def page_holdings() -> None:
    page_header("Enter your holdings",
                "Type them here, or paste a CSV. Nothing is saved anywhere — the file "
                "lives in memory for one request.",
                "Max 60 holdings")

    st.markdown(
        '<div class="card" style="display:flex;gap:26px;align-items:center;'
        'padding:16px 20px"><div style="font-size:12.5px;color:#8A8078;max-width:200px;'
        'line-height:1.5">What each column is for — only the first and last are '
        'required:</div><div>'
        '<span class="tag">Holding name</span>'
        '<span class="tag">Symbol / AMFI code</span>'
        '<span class="tag">Type</span>'
        '<span class="tag">Market</span>'
        '<span class="tag">Risk</span>'
        '<span class="tag">Amount invested ₹</span></div></div>',
        unsafe_allow_html=True)
    st.write("")

    edited = st.data_editor(
        ss.rows, num_rows="dynamic", use_container_width=True, key="editor",
        column_config={
            "Holding name": st.column_config.TextColumn(
                width="large", help="e.g. Parag Parikh Flexi Cap, or Reliance Industries"),
            "Symbol / AMFI code": st.column_config.TextColumn(
                help="Ticker for stocks/ETFs (RELIANCE.NS, IVV) or the 6-digit AMFI code "
                     "for a mutual fund (122639). Leave blank for an unpriceable basket."),
            "Type": st.column_config.SelectboxColumn(options=TYPES, required=False),
            "Market": st.column_config.SelectboxColumn(options=MARKETS, required=False),
            "Risk": st.column_config.SelectboxColumn(
                options=RISKS, help="Your own label — used for the risk-tier split."),
            "Amount invested ₹": st.column_config.NumberColumn(
                format="%d", min_value=0, help="Total you've put in."),
        })
    ss.rows = edited

    csv_text, ready = rows_to_csv(edited)
    st.markdown(
        f'<div style="font-size:12.5px;color:#8A8078;line-height:1.6;margin-top:6px">'
        f'<span class="mono ink">{ready}</span> of '
        f'<span class="mono ink">{len(edited)}</span> rows usable. Amounts alone give you '
        f'tier <span class="mono ink">L1</span> — allocation, diversification, '
        f'concentration and overlap. To unlock exact values, XIRR and risk '
        f'(<span class="mono ink">L2</span>) you need month-by-month SIP amounts, which '
        f'the paste box below accepts.</div>', unsafe_allow_html=True)
    st.write("")

    c1, c2, c3 = st.columns([1.3, 1, 1.4])
    with c1:
        if st.button("Analyse my portfolio →", type="primary", use_container_width=True,
                     disabled=ready == 0):
            load_analysis(csv_text, "Your portfolio (not stored)")
            st.rerun()
    with c2:
        st.download_button("Blank template", data=csv_text if ready else
                           "Mode,App,Type,Market,Risk,Where,symbol,source,"
                           "Estimated Returns (3Y),Total Invested,Mar'25,April'25\n"
                           "Equity,Groww,Share,India,Med,Reliance Industries,"
                           "RELIANCE.NS,yfinance,-,15000,5000,10000\n",
                           file_name="portfolio_template.csv", mime="text/csv",
                           use_container_width=True)
    with c3:
        if st.button("Cancel", use_container_width=True):
            ss.page = "analysis" if ss.analysis else "ask"
            st.rerun()

    with st.expander("Paste or upload a full CSV instead (unlocks tier L2)"):
        st.markdown('<div style="font-size:12.5px;color:#6E655C;line-height:1.6">'
                    'Same columns as the template, plus one column per month named like '
                    '<span class="mono">Mar\'25</span>, <span class="mono">April\'25</span>'
                    ' … holding that month\'s SIP amount. That history is what lets units '
                    'be reconstructed exactly (units = amount ÷ NAV on that date) instead '
                    'of estimated.</div>', unsafe_allow_html=True)
        up = st.file_uploader("CSV file", type=["csv"], key="up")
        pasted = st.text_area("…or paste CSV text", height=120, key="paste")
        if st.button("Analyse this CSV"):
            payload = (up.getvalue().decode("utf-8", "replace") if up
                       else (pasted or "").strip())
            if not payload:
                st.warning("Upload or paste a CSV first.")
            else:
                load_analysis(payload, "Your portfolio (not stored)")
                st.rerun()


# ---------------------------------------------------------------- render

{"ask": page_ask, "analysis": page_analysis, "holdings": page_holdings}[ss.page]()

code, stats = call_json("GET", "/stats")
if code == 200 and stats.get("queries"):
    st.markdown(
        f'<div style="border-top:1px solid #EDE6DC;margin-top:34px;padding-top:14px;'
        f'font-size:11.5px;color:#948A80">Live economics from this server\'s own log: '
        f'<span class="mono ink">{stats["queries"]}</span> queries served · avg '
        f'<span class="mono ink">₹{stats["avg_cost_inr"]:.2f}</span> per query · '
        f'<span class="mono ink">{stats.get("reports_generated", 0)}</span> AI reports at '
        f'avg <span class="mono ink">₹{stats.get("avg_report_cost_inr", "—")}</span>. '
        f'Free-demo limits: 20 chat turns and 3 reports per day.</div>',
        unsafe_allow_html=True)
