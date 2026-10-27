"""Portfolio X-Ray — Streamlit frontend, restyled to the "See through your portfolio" redesign.

Three surfaces behind a dark rail: Ask the analyst · Portfolio analysis · Enter holdings.
Design tokens (obsidian #0B0C0E, graphite surfaces, chartreuse #D4FF3F for actions, gains and
the active state, ice #9AD7FF for data; Instrument Serif / Schibsted Grotesk / Geist Mono).
This is a visual redesign only: the pages, controls, endpoints and wording are unchanged.

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
import uuid

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
# Mirrors storage.TTL_DAYS. Duplicated rather than imported: the frontend must not import
# backend modules, because it is deployed separately and has no database credential.
STORAGE_TTL_DAYS = 90
UNREACHABLE = ("The analysis server is unreachable. It sleeps after 15 idle minutes on the "
               "free tier — wait ~30s and try again. If it keeps failing, the backend is "
               "down rather than asleep.")


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


# ---------------------------------------------------------------- saved portfolios

def owner_key(create: bool = False) -> str:
    """The secret that owns your saved portfolios, kept in the URL as `?k=…`.

    Streamlit has no writable browser storage — session state dies on refresh, which is
    the very problem saving exists to solve, and `st.context.cookies` is read-only. A
    query parameter is the one place a value survives a refresh without adding a
    third-party component, and it has the useful property of being visible: this is a
    capability, and anyone you send the URL to gets your saved portfolios with it.

    Minted on the first SAVE, not on first visit, so the URL of a reader who never saves
    anything is left alone.
    """
    k = st.query_params.get("k", "")
    if not k and create:
        k = uuid.uuid4().hex
        st.query_params["k"] = k
    return k


def saved_list() -> tuple[bool, list]:
    """(storage_enabled, rows). Cached in session state, because the rail re-renders on
    EVERY interaction and this is a database round trip to another continent.

    Measured before caching: 0.6-1.0s per call with a key, against 0.005s without one —
    the early return below is why a visitor who never saves pays nothing. Left uncached,
    every chip click and page switch would have carried a second of Singapore latency.

    The list only changes when this browser saves or deletes, and both paths clear the
    cache, so there is nothing to go stale except a second tab — which a reload fixes.

    Both failure modes return storage-off rather than raising, so a paused Supabase
    project degrades the app to session-only instead of breaking the rail.
    """
    if ss.get("saved_cache") is not None:
        return ss.saved_cache
    k = owner_key()
    if not k:
        return True, []                      # no key yet: nothing to fetch, nothing to cache
    code, data = call_json("GET", "/portfolios", headers={"X-Owner-Key": k})
    out = ((False, []) if code != 200 or not isinstance(data, dict)
           else (bool(data.get("enabled")), data.get("portfolios") or []))
    ss.saved_cache = out
    return out


def saved_save(name: str, csv_text: str) -> str | None:
    code, data = call_json("POST", "/portfolios", headers={"X-Owner-Key": owner_key(True)},
                           json={"name": name, "csv_text": csv_text})
    ss.saved_cache = None
    return None if code == 200 else (data.get("error") or "Could not save.")


def saved_load(pid: str) -> dict | None:
    code, data = call_json("GET", f"/portfolios/{pid}", headers={"X-Owner-Key": owner_key()})
    return data if code == 200 else None


def saved_delete(pid: str) -> None:
    call_json("DELETE", f"/portfolios/{pid}", headers={"X-Owner-Key": owner_key()})
    ss.saved_cache = None


# ---------------------------------------------------------------- formatting helpers

# Formatting lives in uiformat.py — pure and unit-tested (no Streamlit import).
from uiformat import inr, md_to_html, pct  # noqa: E402


# ---------------------------------------------------------------- design system (CSS)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400..600&family=Instrument+Serif:ital@0;1&family=Schibsted+Grotesk:ital,wght@0,400..800;1,400..800&display=swap');

:root {
  --bg0: #0B0C0E; --rail: #0E1013; --bg1: #111418; --bg2: #161A1F; --bg3: #1C2026;
  --line: #22262D; --line2: #2D323A;
  --ink1: #ECEFF2; --ink2: #B4BBC3; --ink3: #8C949D; --ink4: #5E656E;
  --model: #C6DCEC; --lime: #D4FF3F; --lime-ink: #0B0C0E;
  --ice: #9AD7FF; --ice-glow: #8CCBF5; --coral: #FF7D61; --amber: #F4C152;
  --serif: 'Instrument Serif', 'Iowan Old Style', Georgia, serif;
  --sans: 'Schibsted Grotesk', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  --mono: 'Geist Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}
html, body, .stApp, button, input, textarea, select {
  font-family: var(--sans) !important;
}
.stApp { background: var(--bg0); color: var(--ink1); -webkit-font-smoothing: antialiased; }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer, header [data-testid="stStatusWidget"] { visibility: hidden; }
.block-container { padding: 2rem 2.6rem 3rem; max-width: 1240px; }
.mono { font-family: var(--mono); }
:focus-visible { outline: 2px solid var(--lime) !important; outline-offset: 2px; }

/* ---------- rail ---------- */
[data-testid="stSidebar"] { border-right: 1px solid var(--line); }
[data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebarContent"] { background: var(--rail); }
[data-testid="stSidebar"] .stButton > button {
  width: 100%; text-align: left; justify-content: flex-start; min-height: 38px;
  border-radius: 8px; font-size: 14px; font-weight: 500; padding: 0 12px;
  background: transparent; color: var(--ink2); border: none; box-shadow: none;
}
[data-testid="stSidebar"] .stButton > button div { justify-content: flex-start; }
[data-testid="stSidebar"] .stButton > button p { font-size: 14px; }
[data-testid="stSidebar"] .stButton > button:hover { background: var(--bg2); color: var(--ink1); }
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background: var(--bg2); color: var(--ink1); font-weight: 600;
  box-shadow: inset 3px 0 0 var(--lime);
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] [data-testid="stIconMaterial"] {
  color: var(--lime);
}
[data-testid="stSidebar"] [class*="st-key-rail_sample"] .stButton > button {
  justify-content: center; background: var(--lime); color: var(--lime-ink);
  box-shadow: none; font-weight: 600; min-height: 42px;
}
[data-testid="stSidebar"] [class*="st-key-rail_sample"] .stButton > button div {
  justify-content: center;
}
[data-testid="stSidebar"] [class*="st-key-rail_sample"] .stButton > button:hover {
  background: #E2FF7A;
}
.rail-brand { display: flex; align-items: center; gap: 10px; color: var(--ink1); }
.rail-brand .wm { display: flex; align-items: baseline; gap: 5px; }
.rail-brand .wm b { font-family: var(--sans); font-size: 15px; font-weight: 600;
  letter-spacing: -.01em; color: var(--ink1); }
.rail-brand .wm i { font-family: var(--serif); font-size: 21px; font-style: italic;
  color: var(--ink1); }
.rail-sub { font-size: 12.5px; color: var(--ink3); margin: 6px 0 0 36px; }
.rail-label { font-family: var(--mono); font-size: 11px; font-weight: 500;
  letter-spacing: .12em; text-transform: uppercase; color: var(--ink3); margin: 4px 0 12px; }
.rail-value { font-size: 26px; font-weight: 500; letter-spacing: -.02em; color: var(--ink1);
  font-variant-numeric: tabular-nums; line-height: 1.05; }
.rail-delta { font-size: 12.5px; margin: 6px 0 12px; font-variant-numeric: tabular-nums; }
.rail-cap { font-size: 11.5px; color: var(--ink3); margin-top: 8px; }
.rail-hr { height: 1px; background: var(--line); margin: 18px 0; }
.rail-card { border: 1px solid var(--line2); border-radius: 10px; background: var(--bg1);
  padding: 13px 14px; }
.rail-card .t { font-size: 13.5px; font-weight: 600; color: var(--ink1); }
.rail-card .b { font-size: 12.5px; color: var(--ink3); line-height: 1.5; margin-top: 5px; }
.rail-foot { font-size: 12px; color: var(--ink3); line-height: 1.55; margin-top: 16px; }

/* ---------- portfolio switcher ---------- */
.sw { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.sw-l { font-family: var(--mono); font-size: 11px; font-weight: 500; letter-spacing: .12em;
  text-transform: uppercase; color: var(--ink3); }
.block-container [class*="st-key-pf_"] .stButton > button {
  min-height: 36px; border-radius: 999px; font-size: 13.5px; font-weight: 500;
  border: 1px solid var(--line2); background: var(--bg1); color: var(--ink2);
}
.block-container [class*="st-key-pf_"] .stButton > button:hover {
  background: var(--bg2); color: var(--ink1); border-color: var(--ink4);
}
/* The active book is stated in the same lime the rail uses for the active page, so
   "which portfolio am I looking at" reads the same way as "which page am I on". */
.block-container [class*="st-key-pf_"] .stButton > button[kind="primary"] {
  background: var(--lime); color: var(--lime-ink); border-color: var(--lime);
  font-weight: 600;
}
.block-container [class*="st-key-pf_"] .stButton > button[kind="primary"]:hover {
  background: #E2FF7A; color: var(--lime-ink);
}
.block-container [class*="st-key-pf_del"] .stButton > button {
  min-height: 32px; border-radius: 8px; font-size: 12.5px; color: var(--ink3);
  background: transparent; border: 1px solid var(--line);
}
.block-container [class*="st-key-pf_del"] .stButton > button:hover {
  color: var(--coral); border-color: var(--coral); background: transparent;
}
/* The uploader is the SECOND of two equal ways to add a portfolio, so it is sized like
   the first rather than left as Streamlit's small inset button in a tall dropzone. */
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
  padding: 8px; min-height: 0; background: var(--bg1);
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] small,
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] svg { display: none; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] > div { width: 100%; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
  width: 100%; min-height: 38px; border-radius: 8px; font-size: 14px; font-weight: 500;
  border: 1px solid var(--line2); background: transparent; color: var(--ink1);
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button:hover {
  background: var(--bg3); border-color: var(--ink4); color: var(--ink1);
}

/* ---------- page header ---------- */
.page-h { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 16px 24px; flex-wrap: wrap; border-bottom: 1px solid var(--line);
  padding-bottom: 20px; margin-bottom: 26px; }
.page-h h1 { font-family: var(--serif) !important; font-size: 44px !important;
  font-weight: 400 !important; line-height: 1 !important; letter-spacing: -.015em;
  margin: 0 !important; padding: 0 !important; color: var(--ink1) !important; }
.page-h h1 a, .page-h [data-testid="stHeaderActionElements"] { display: none !important; }
.page-h .s { font-size: 14px; color: var(--ink3); margin-top: 10px; }
.pill { display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 12px;
  border: 1px solid var(--line2); border-radius: 8px; background: var(--bg1);
  font-size: 13px; font-weight: 500; color: var(--ink1); white-space: nowrap; }
.pill .dot { width: 8px; height: 8px; border-radius: 2px; background: var(--ice); }

/* ---------- panels and figures ---------- */
.card { background: var(--bg1); border: 1px solid var(--line2); border-radius: 14px;
  padding: 20px 22px; }
.kpis { display: grid; grid-template-columns: minmax(0, 1.5fr) repeat(3, minmax(0, 1fr));
  border: 1px solid var(--line2); border-radius: 14px; background: var(--bg1);
  overflow: hidden; }
.kpi { display: flex; flex-direction: column; min-width: 0; padding: 20px 22px;
  border-left: 1px solid var(--line); }
.kpi:first-child { border-left: 0; background: var(--bg2); }
.k-label { font-size: 13px; color: var(--ink3); }
.k-big { font-size: 40px; font-weight: 500; letter-spacing: -.03em; color: var(--ink1);
  line-height: 1.05; margin-top: 8px; font-variant-numeric: tabular-nums; }
.k-num { font-size: 30px; font-weight: 500; letter-spacing: -.02em; color: var(--ink1);
  line-height: 1.1; margin-top: 8px; font-variant-numeric: tabular-nums; }
.k-num .u { font-size: 15px; font-weight: 400; letter-spacing: 0; color: var(--ink3); }
.k-note { font-size: 13px; color: var(--ink2); margin-top: 6px; line-height: 1.45; }
.k-foot { font-size: 12px; color: var(--ink3); margin-top: auto; padding-top: 14px;
  line-height: 1.5; }
.up { color: var(--lime); } .down { color: var(--coral); } .warn { color: var(--amber); }
.muted { color: var(--ink3); } .ink { color: var(--ink1); }
.sec-t { font-size: 15px; font-weight: 600; color: var(--ink1); }
.sec-s { font-family: var(--mono); font-size: 11px; font-weight: 500; letter-spacing: .08em;
  text-transform: uppercase; color: var(--ink3); }
.sec-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.hr { height: 1px; background: var(--line); margin: 18px 0; }
@media (max-width: 1100px) {
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi:nth-child(3) { border-left: 0; }
  .kpi:nth-child(n+3) { border-top: 1px solid var(--line); }
}
@media (max-width: 640px) {
  .kpis { grid-template-columns: minmax(0, 1fr); }
  .kpi { border-left: 0 !important; }
  .kpi + .kpi { border-top: 1px solid var(--line); }
  .page-h h1 { font-size: 34px !important; }
}

/* ---------- small charts ---------- */
.spark { position: relative; width: 100%; }
.spark svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.spark polyline { fill: none; stroke: var(--ice-glow); stroke-width: 2;
  vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
.spark line { stroke: var(--ink4); stroke-width: 1; stroke-dasharray: 3 4;
  vector-effect: non-scaling-stroke; }
.spark .end { position: absolute; right: 0; width: 8px; height: 8px; border-radius: 50%;
  transform: translate(50%, -50%); }
.duo { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 7px 10px;
  align-items: center; margin-top: 14px; font-size: 11.5px; color: var(--ink3); }
.track { height: 6px; border-radius: 3px; background: var(--bg3); overflow: hidden; }
.track > div { height: 6px; border-radius: 3px; background: var(--ice-glow); }
.track.idx > div { background: var(--ink4); }
.pips { display: flex; gap: 3px; margin-top: 14px; }
.pips i { flex: 1; height: 6px; border-radius: 2px; background: var(--bg3); }
.pips i.on { background: var(--ice-glow); }
.stack { display: flex; height: 12px; gap: 2px; margin: 14px 0 10px; }
.stack div:first-child { border-radius: 3px 0 0 3px; }
.stack div:last-child { border-radius: 0 3px 3px 0; }
.legend { display: flex; align-items: center; gap: 10px; font-size: 13.5px;
  padding: 5px 0; color: var(--ink2); }
.legend .sw { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.legend .n { flex: 1; }
.legend .v { font-weight: 500; color: var(--ink1); font-variant-numeric: tabular-nums; }
.note { font-size: 12.5px; color: var(--ink3); line-height: 1.55; margin-top: 12px; }

/* ---------- worth a look ---------- */
.ins-list { background: var(--bg1); border: 1px solid var(--line2); border-radius: 14px;
  overflow: hidden; }
.ins { padding: 15px 18px; border-top: 1px solid var(--line); }
.ins:first-child { border-top: 0; }
.ins .row { display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; margin-bottom: 5px; }
.ins .t { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 600;
  color: var(--ink1); }
.ins .v { font-size: 14px; font-weight: 500; color: var(--ink1); white-space: nowrap;
  font-variant-numeric: tabular-nums; }
.ins .b { font-size: 13.5px; color: var(--ink2); line-height: 1.55; padding-left: 23px; }
.prio { display: inline-flex; align-items: flex-end; gap: 2px; height: 10px; flex: none; }
.prio i { width: 3px; border-radius: 1px; background: var(--ink4); }
.prio i.on { background: var(--ink1); }

/* ---------- holdings table ---------- */
.tbl { background: var(--bg1); border: 1px solid var(--line2); border-radius: 14px;
  overflow: hidden; }
.tbl .hd, .tbl .tr { display: grid;
  grid-template-columns: 2.6fr 1.4fr 2fr 1.2fr 1.2fr; gap: 16px; padding: 13px 20px;
  align-items: center; }
.tbl .hd { font-family: var(--mono); font-size: 11px; font-weight: 500; letter-spacing: .08em;
  text-transform: uppercase; color: var(--ink3); border-bottom: 1px solid var(--line2); }
.tbl .tr { border-top: 1px solid var(--line); font-size: 14px; color: var(--ink1); }
.tbl .hd + .tr { border-top: 0; }
.tbl .r { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .ty { color: var(--ink3); font-size: 13px; }
.tbl .share { display: flex; align-items: center; gap: 10px; }
.tbl .share .track { flex: 1; }
.tbl .share .v { width: 48px; text-align: right; color: var(--ink2);
  font-variant-numeric: tabular-nums; }
.tbl .na { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink3); }
.tbl .dim { color: var(--ink2); }
@media (max-width: 700px) {
  .tbl .hd, .tbl .tr { grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, .9fr)
    minmax(0, 1fr); gap: 10px; padding: 12px 14px; }
  .tbl .ty { display: none; }
  .tbl .share .track { display: none; }
}

/* ---------- chat ---------- */
.intro { display: flex; gap: 14px; max-width: 820px; margin: 2px 0 22px; }
.intro .av { flex: none; margin-top: 3px; }
.intro .prose { color: var(--ink2); }
.msg-user { font-family: var(--serif); font-size: 30px; line-height: 1.12;
  letter-spacing: -.01em; color: var(--ink1); max-width: 820px;
  margin: 26px 0 14px; padding-top: 26px; border-top: 1px solid var(--line); }
.msg-user.first { margin-top: 4px; padding-top: 0; border-top: 0; }
.msg-a { max-width: 820px; margin-bottom: 12px; }
.prose { font-size: 16px; line-height: 1.65; color: var(--model); min-width: 0; }
.prose p { margin: 0 0 10px; }
.prose ul, .prose ol { margin: 6px 0 12px; padding-left: 22px; }
.prose li { margin-bottom: 7px; padding-left: 2px; }
.prose ol { list-style: decimal; }
.prose ol li::marker { font-family: var(--mono); font-size: 13px; color: var(--ink3); }
.prose ul li::marker { color: var(--ice); }
.prose strong { font-weight: 600; color: var(--ink1); }
.prose em { font-style: italic; }
.prose a { color: var(--ice); }
.prose code { font-family: var(--mono); font-size: 13px; background: var(--bg2);
  padding: 1px 6px; border-radius: 4px; color: var(--ink1); }
.boundary { display: flex; gap: 12px; align-items: flex-start; max-width: 820px;
  padding: 16px 18px; border: 1px solid rgba(154, 215, 255, .3); border-radius: 12px;
  background: rgba(154, 215, 255, .04); }
.boundary svg { flex: none; margin-top: 4px; }
.boundary .prose { color: var(--ink1); }
.boundary .prose p:last-child { margin-bottom: 0; }
.tools { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; max-width: 820px;
  margin: 0 0 8px; font-size: 12.5px; color: var(--ink3); }
.tools .sq { width: 8px; height: 8px; background: var(--lime); flex: none; }
.tag { display: inline-flex; align-items: center; height: 24px; padding: 0 9px;
  border: 1px solid var(--line2); border-radius: 6px; background: var(--bg2);
  font-family: var(--mono); font-size: 12px; color: var(--ink2); margin: 2px 4px 2px 0; }
.refused { font-size: 12.5px; color: var(--ink3); line-height: 1.55; max-width: 820px;
  margin: 10px 0 8px; }
.thinking { display: flex; flex-direction: column; gap: 10px; max-width: 820px;
  font-size: 13.5px; color: var(--ink2); margin: 2px 0 12px; }
.thinking .scan { height: 2px; border-radius: 1px; background-color: var(--bg3);
  background-image: linear-gradient(90deg, transparent, var(--ice-glow), transparent);
  background-repeat: no-repeat; background-size: 35% 100%;
  animation: xr-scan 1.6s cubic-bezier(.4, 0, .2, 1) infinite; }
@keyframes xr-scan { from { background-position: -40% 0; } to { background-position: 140% 0; } }
@media (prefers-reduced-motion: reduce) {
  .thinking .scan { animation: none; background-position: 30% 0; }
}
@media (max-width: 640px) { .msg-user { font-size: 25px; } }

/* ---------- Streamlit widgets ---------- */
.block-container .stButton > button {
  min-height: 42px; border-radius: 10px; border: 1px solid var(--line2);
  background: transparent; color: var(--ink1); font-size: 14px; font-weight: 500;
}
.block-container .stButton > button:hover { background: var(--bg3); border-color: #3A404A;
  color: var(--ink1); }
.block-container .stButton > button[kind="primary"] {
  background: var(--lime); color: var(--lime-ink); border: none; font-weight: 600;
}
.block-container .stButton > button[kind="primary"]:hover { background: #E2FF7A;
  color: var(--lime-ink); }
.block-container .stButton > button:disabled { opacity: .45; }
.block-container [class*="st-key-chip_"] .stButton > button {
  border-radius: 999px; color: var(--ink2); font-size: 13.5px; min-height: 40px;
}
.block-container [class*="st-key-chip_"] .stButton > button:hover { color: var(--ink1);
  border-color: var(--ink4); background: var(--bg2); }
.stDownloadButton > button { min-height: 42px; border-radius: 10px;
  border: 1px solid var(--line2); background: transparent; color: var(--ink1);
  font-size: 14px; font-weight: 500; }
.stDownloadButton > button:hover { background: var(--bg3); border-color: #3A404A;
  color: var(--ink1); }
[data-testid="stBottom"], [data-testid="stBottom"] > div,
[data-testid="stBottomBlockContainer"] { background: var(--bg0); }
[data-testid="stChatInput"] { border-radius: 14px !important; }
[data-testid="stChatInput"] textarea { font-size: 15.5px !important; }
[data-testid="stChatInput"] textarea::placeholder { color: var(--ink3) !important; opacity: 1; }
[data-testid="stChatInputSubmitButton"] { border-radius: 10px !important; }
div[data-testid="stExpander"] details { border: 1px solid var(--line2); border-radius: 12px;
  background: var(--bg1); }
[data-testid="stFileUploaderDropzone"] { background: var(--bg0); border: 1px dashed var(--line2);
  border-radius: 10px; }
[data-testid="stCaptionContainer"] { color: var(--ink3); }
.foot { border-top: 1px solid var(--line); margin-top: 40px; padding-top: 16px;
  font-size: 12px; color: var(--ink3); line-height: 1.6; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

INK, LIME, CORAL, ICE, MUTED = "#ECEFF2", "#D4FF3F", "#FF7D61", "#8CCBF5", "#5E656E"

# Categorical colours follow the entity (a market or asset class), never its rank, so the
# same slice keeps its colour across both bars and across portfolios.
_V_US, _V_INDIA, _V_GLOBAL, _V_MF, _V_GOLD = "#3B9BE0", "#BC8424", "#8F7EF0", "#22A88A", "#E0685A"

MARK_SVG = (
    '<svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">'
    '<path d="M2 9V4.5A2.5 2.5 0 0 1 4.5 2H9M23 2h4.5A2.5 2.5 0 0 1 30 4.5V9M30 23v4.5a2.5 2.5 '
    '0 0 1-2.5 2.5H23M9 30H4.5A2.5 2.5 0 0 1 2 27.5V23" fill="none" stroke="#9AD7FF" '
    'stroke-opacity=".75" stroke-width="2" stroke-linecap="round"></path>'
    '<rect x="14" y="6" width="4" height="20" rx="2" transform="rotate(45 16 16)" '
    'fill="#9AD7FF" fill-opacity=".5"></rect>'
    '<rect x="14" y="6" width="4" height="20" rx="2" transform="rotate(-45 16 16)" '
    'fill="#9AD7FF" fill-opacity=".5"></rect>'
    '<rect x="14" y="14" width="4" height="4" transform="rotate(45 16 16)" fill="#D4FF3F">'
    '</rect></svg>')
SHIELD_SVG = ('<svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="#9AD7FF" '
              'stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M9 2 '
              '3.5 4.2v4.3c0 3.4 2.3 6.1 5.5 7.5 3.2-1.4 5.5-4.1 5.5-7.5V4.2Z"></path></svg>')
SLASH_SVG = ('<svg width="14" height="14" viewBox="0 0 18 18" fill="none" stroke="#8C949D" '
             'stroke-width="1.6" aria-hidden="true" style="flex:none"><circle cx="9" cy="9" '
             'r="6.5"></circle><path d="m4.5 13.5 9-9"></path></svg>')


# ---------------------------------------------------------------- components

def sparkline(series: list, height: int = 40, dark: bool = True) -> str:
    """A line of real month-end portfolio value. The dashed line marks where the window
    started; the end dot is lime when the portfolio is above that start, coral when below."""
    vals = [s["value"] for s in series if s.get("value")]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1

    def y(v):
        return round(88 - (v - lo) / span * 76, 2)

    n = len(vals)
    pts = " ".join(f"{i / (n - 1) * 100:.2f},{y(v)}" for i, v in enumerate(vals))
    end = LIME if vals[-1] >= vals[0] else CORAL
    ring = "#0E1013" if dark else "#161A1F"
    return (f'<div class="spark" style="height:{height}px">'
            f'<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
            f'<line x1="0" y1="{y(vals[0])}" x2="100" y2="{y(vals[0])}"></line>'
            f'<polyline points="{pts}"></polyline></svg>'
            f'<span class="end" style="top:{y(vals[-1])}%;background:{end};'
            f'box-shadow:0 0 0 2px {ring}"></span></div>')


def _seg_color(name: str) -> str:
    n = name.lower()
    if "gold" in n or "silver" in n:
        return _V_GOLD
    if n.startswith("mf"):
        return _V_MF
    if "crypto" in n or "global" in n:
        return _V_GLOBAL
    if "india" in n:
        return _V_INDIA
    if n == "us" or n.endswith(" us"):
        return _V_US
    return MUTED


def stacked(pairs: list) -> str:
    total = sum(v for _, v in pairs) or 1
    seg = "".join(f'<div style="width:{v / total * 100:.2f}%;'
                  f'background:{_seg_color(str(n))}"></div>'
                  for n, v in pairs)
    rows = "".join(
        f'<div class="legend"><span class="sw" style="background:'
        f'{_seg_color(str(n))}"></span>'
        f'<span class="n">{_html.escape(str(n))}</span>'
        f'<span class="v">{v:.1f}%</span></div>'
        for n, v in pairs)
    return f'<div class="stack">{seg}</div>{rows}'


def insight(title: str, value: str, body: str, tone: str = "medium") -> str:
    tone_cls = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(tone, tone)
    level = {"high": 3, "medium": 2, "low": 1}.get(tone_cls, 2)
    bars = "".join(f'<i class="{"on" if i < level else ""}" style="height:{4 + 3 * i}px"></i>'
                   for i in range(3))
    return (f'<div class="ins {tone_cls}"><div class="row">'
            f'<div class="t"><span class="prio" role="img" aria-label="{tone_cls} priority" '
            f'title="{tone_cls} priority">{bars}</span>{_html.escape(title)}</div>'
            f'<div class="v">{_html.escape(value)}</div></div>'
            f'<div class="b">{_html.escape(body)}</div></div>')


def portfolio_switcher() -> None:
    """The book the whole app is about, switchable in one click, at the top of every page.

    Buttons rather than st.radio or a segmented control: a radio keeps its own state under
    its key, that state beats the `index=` argument, and writing to the key to keep them in
    step raises StreamlitAPIException once the widget has been created earlier in the same
    run — which the rail always has. Buttons hold no state to fight.

    Switching only re-points ss.analysis at an already-fetched result, so it costs nothing
    and refetches nothing. Both pages read ss.analysis and ss.csv_text, so the dashboard
    and the analyst move together by construction rather than by remembering to.
    """
    if not ss.portfolios:
        return
    names = list(ss.portfolios)
    st.markdown('<div class="sw"><span class="sw-l">Portfolio</span></div>',
                unsafe_allow_html=True)
    # Fixed-width columns so pills keep a readable size rather than stretching to fill the
    # row: two portfolios should not become two half-page buttons. Remove takes the slot
    # straight after the last pill, so it reads as part of the same control instead of
    # claiming a row of its own above the page title.
    per_row = 4
    cells = names + (["·remove·"] if len(names) > 1 else [])
    for start in range(0, len(cells), per_row):
        chunk = cells[start:start + per_row]
        cols = st.columns(per_row)
        for col, name in zip(cols, chunk):
            with col:
                if name == "·remove·":
                    if st.button("Remove", key="pf_del", use_container_width=True,
                                 icon=":material/close:",
                                 help=f"Forget “{ss.active}” in this browser session"):
                        ss.portfolios.pop(ss.active, None)
                        activate_portfolio(next(iter(ss.portfolios)))
                        st.rerun()
                elif st.button(name, key=f"pf_{name}", use_container_width=True,
                               type="primary" if name == ss.active else "secondary"):
                    activate_portfolio(name)
                    st.rerun()


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
# Several named portfolios, so one person can compare their own book against a sample, a
# spouse's, or a what-if. Held ONLY in this browser session: nothing is written server
# side, which is what keeps the "nothing you upload is stored" promise true. They are lost
# on refresh, and that is the trade being made deliberately.
ss.setdefault("portfolios", {})      # name -> {csv_text, analysis, label}
ss.setdefault("active", None)
ss.setdefault("rail_upload_sig", None)   # the last file the rail uploader tried
ss.setdefault("saved_cache", None)       # (enabled, rows); None = refetch
ss.setdefault("chat", [])
ss.setdefault("queued", None)
ss.setdefault("pending", None)   # question awaiting an answer (already shown in the thread)
ss.setdefault("report", None)
ss.setdefault("rows", pd.DataFrame([
    {"Holding name": "", "Symbol / AMFI code": "", "Type": "Share", "Market": "India",
     "Risk": "Med", "Amount invested ₹": None} for _ in range(4)]))

TYPES = ["Share", "ETF", "Mutual Fund", "Crypto", "Basket"]
MARKETS = ["India", "US", "Global"]
RISKS = ["Low", "Med", "High"]


def activate_portfolio(name: str) -> None:
    """Point the rest of the app at one of the loaded portfolios.

    Everything downstream still reads ss.analysis / ss.csv_text, so switching costs
    nothing and refetches nothing — the analysis was already paid for when it loaded.

    The chat is cleared on purpose. A thread is about one book: carrying "your biggest
    position is Reliance at 26%" over to a portfolio that does not hold Reliance would
    put stale figures under a new heading, and the model would answer follow-ups from
    them. The report is dropped for the same reason.
    """
    entry = ss.portfolios.get(name)
    if not entry:
        return
    ss.active = name
    ss.analysis = entry["analysis"]
    ss.csv_text = entry["csv_text"]
    ss.source_label = entry["label"]
    ss.report = None
    ss.chat = []


def unique_name(base: str) -> str:
    """Loading two files both called "portfolio" must not silently replace the first."""
    name = (base or "Portfolio").strip() or "Portfolio"
    if name not in ss.portfolios:
        return name
    i = 2
    while f"{name} ({i})" in ss.portfolios:
        i += 1
    return f"{name} ({i})"


def load_analysis(csv_text: str | None, label: str, name: str | None = None) -> bool:
    """True if the portfolio loaded. The caller MUST NOT rerun on False.

    It used to return None and every caller rerun unconditionally, which repainted the page
    and wiped the st.error before anyone could read it — so a sleeping backend looked like a
    dead button: click, nothing happens, no reason given. Streamlit has no other way to keep
    a message on screen; surviving the rerun is the whole mechanism.
    """
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
        key = unique_name(name or label)
        ss.portfolios[key] = {"csv_text": csv_text, "analysis": data, "label": label}
        activate_portfolio(key)
        ss.page = "analysis"
        return True
    # code 0 means the request never reached a server, so `data["error"]` is requests'
    # own exception text — "HTTPConnectionPool(host=…): Max retries exceeded … WinError
    # 10061". Surfacing that was the whole point of the fix above and it is still the
    # wrong thing to show: on the free tier the overwhelmingly likely cause is a sleeping
    # backend, which the reader can act on. A real HTTP reply DOES carry a message worth
    # showing ("Portfolio file not found", the CSV validation errors), so that one is kept.
    st.error(UNREACHABLE if code == 0 else
             data.get("error", UNREACHABLE))
    return False


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
    st.markdown(f'<div class="rail-brand">{MARK_SVG}<span class="wm"><b>Portfolio</b>'
                f'<i>X-Ray</i></span></div>'
                '<div class="rail-sub">A clearer look at what you own</div>',
                unsafe_allow_html=True)
    st.write("")
    nav = [("ask", "Ask the analyst", ":material/forum:"),
           ("analysis", "Portfolio analysis", ":material/space_dashboard:"),
           ("holdings", "Enter my holdings", ":material/edit_note:")]
    for key, label, icon in nav:
        if st.button(label, key=f"nav_{key}", icon=icon, use_container_width=True,
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
                        f'<div class="rail-cap">Last {len(tl)} months, reconstructed</div>',
                        unsafe_allow_html=True)
        st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="rail-card"><div class="t">{_html.escape(ss.source_label or "")}'
            f'</div><div class="b">{ss.analysis["holdings_priced"]} of '
            f'{ss.analysis["holdings_total"]} holdings priced · input tier '
            f'{ss.analysis["level"]}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="rail-card"><div class="t">Nothing loaded yet</div>'
                    '<div class="b">Start with the sample portfolio, or bring your '
                    'own.</div></div>', unsafe_allow_html=True)

    # --- adding a portfolio ---------------------------------------------------------
    # Both routes live here, together, and stay here once something is loaded. Before,
    # the sample button vanished after the first load, so there was no way to pull the
    # sample in as a SECOND portfolio to compare your own book against — which is most
    # of the point of having several.
    st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
    st.markdown('<div class="rail-label">Add a portfolio</div>', unsafe_allow_html=True)

    if "Sample" not in ss.portfolios:
        if st.button("Load the sample", key="rail_sample", use_container_width=True,
                     type="primary" if not ss.portfolios else "secondary"):
            if load_analysis(None, "Sample portfolio", name="Sample"):
                st.rerun()

    up = st.file_uploader("Upload a CSV", type=["csv"], key="rail_upload",
                          label_visibility="collapsed")
    if up is not None:
        # Attempt each distinct file ONCE. The widget keeps handing the same file back on
        # every rerun, so without this a rejected CSV would retry forever and the error
        # it produced would be redrawn on top of itself.
        sig = (up.name, up.size)
        if sig != ss.rail_upload_sig:
            ss.rail_upload_sig = sig
            stem = os.path.splitext(up.name)[0][:28] or "Portfolio"
            if load_analysis(up.getvalue().decode("utf-8", "replace"),
                             "Your portfolio (not stored)", name=stem):
                st.rerun()
    st.markdown('<div class="rail-cap">A Groww equity or mutual-fund transaction report, '
                'or the SIP sheet.</div>', unsafe_allow_html=True)

    # --- saved portfolios -----------------------------------------------------------
    enabled, saved = saved_list()
    if enabled:
        st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
        st.markdown('<div class="rail-label">Saved</div>', unsafe_allow_html=True)

        if ss.analysis and ss.csv_text:
            # Only an uploaded book can be saved. The sample is already one click away and
            # a copy of it would just be clutter in a list capped at a dozen.
            if st.button("Save this portfolio", key="save_pf", use_container_width=True,
                         icon=":material/bookmark_add:"):
                err = saved_save(ss.active or "Portfolio", ss.csv_text)
                if err:
                    st.error(err)
                else:
                    st.rerun()

        for row in saved:
            c1, c2 = st.columns([4, 1])
            with c1:
                if st.button(row["name"], key=f"sv_{row['id']}", use_container_width=True,
                             icon=":material/folder_open:",
                             help=f"Saved {row['created_at'][:10]}"):
                    got = saved_load(row["id"])
                    if got and load_analysis(got["csv_text"], "Saved portfolio",
                                             name=got["name"]):
                        st.rerun()
                    elif not got:
                        st.error("That portfolio could not be loaded.")
            with c2:
                if st.button("", key=f"rm_{row['id']}", use_container_width=True,
                             icon=":material/delete:", help="Delete permanently"):
                    saved_delete(row["id"])
                    st.rerun()

        if not saved:
            st.markdown('<div class="rail-cap">Nothing saved yet. Load a portfolio of '
                        'your own, then save it to find it here next time.</div>',
                        unsafe_allow_html=True)
        else:
            # The URL IS the key. Saying so is the only honest way to offer this without
            # accounts: there is no password to steal and no way to recover a lost link.
            st.markdown('<div class="rail-cap">Bookmark this page — the link is what '
                        'brings these back, and anyone you send it to can see them. '
                        f'Kept {STORAGE_TTL_DAYS} days.</div>', unsafe_allow_html=True)

    st.markdown('<div class="rail-foot">Educational only — not advice, not '
                'SEBI-registered. Portfolios you upload are analysed and discarded; only '
                'the ones you explicitly save are kept.</div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------- page: ask

def page_ask() -> None:
    portfolio_switcher()
    n = ss.analysis["holdings_total"] if ss.analysis else None
    page_header("Ask the analyst",
                "Plain questions about your portfolio. Plain answers, with the numbers "
                "behind them.",
                f"Reading your {n} holdings" if n else "No portfolio loaded yet")

    if not ss.chat:
        st.markdown(
            f'<div class="intro"><div class="av">{MARK_SVG}</div><div class="prose">'
            '<p>Ask me what you actually own. I compute every number in Python — '
            'concentration, exact XIRR from your SIP dates, fund overlap, beta — and I '
            'explain what it means.</p><p>I will not tell you what to buy or sell, and I '
            'will not predict prices. That boundary is enforced in code, not just '
            'promised: try the third suggestion below.</p></div></div>',
            unsafe_allow_html=True)

    first = True
    for role, text, meta in ss.chat:
        if role == "user":
            st.markdown(f'<div class="msg-user{" first" if first else ""}">'
                        f'{_html.escape(text)}</div>', unsafe_allow_html=True)
            first = False
        elif meta.get("refused"):
            st.markdown(f'<div class="boundary">{SHIELD_SVG}'
                        f'<div class="prose">{md_to_html(text)}</div></div>',
                        unsafe_allow_html=True)
            st.markdown(
                '<div class="refused">That refusal is the product working, not '
                'failing: the router classifies advice-seeking questions and answers '
                'them from a fixed script before any tool runs. 100% refusal '
                'correctness on 52 labelled cases — see the README eval table.</div>',
                unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="msg-a"><div class="prose">{md_to_html(text)}</div>'
                        f'</div>', unsafe_allow_html=True)
            if meta.get("tools"):
                tags = "".join(f'<span class="tag">{_html.escape(t)}</span>'
                               for t in meta["tools"])
                st.markdown(f'<div class="tools"><span class="sq"></span>Python tools '
                            f'used: {tags}</div>', unsafe_allow_html=True)

    # A pending question is ALREADY in the thread above (appended before the rerun), so the
    # user sees what they asked while we fetch. The answer lands on the next rerun.
    if ss.pending:
        st.markdown('<div class="thinking"><div class="scan"></div>'
                    '<span>Routing → Python tools → answer</span></div>',
                    unsafe_allow_html=True)
        slot = st.empty()
        # History excludes the pending question — the backend appends it itself.
        history = [{"role": r, "content": t} for r, t, _ in ss.chat[:-1]]
        payload = {"message": ss.pending, "history": history}
        # Without this the backend falls back to the SERVER's default portfolio, so the
        # analyst answers about the sample while the dashboard beside it shows the user's
        # own book — two different portfolios described as one. /chat accepts the CSV in
        # the body exactly as /analysis does; the file lives in memory for one request and
        # is deleted in a finally, which is what keeps "nothing is stored" true.
        if ss.csv_text:
            payload["portfolio_csv"] = ss.csv_text
        with st.spinner("Working…"):
            code, data = call_json("POST", "/chat", cold_slot=slot, json=payload)
        slot.empty()
        answer = data.get("answer", "The backend is unreachable — give the free tier "
                                    "~30s and retry.")
        ss.pending = None
        ss.chat.append(("assistant", answer,
                        {"refused": data.get("refused"),
                         "tools": data.get("tools_used") or []}))
        st.rerun()

    st.markdown('<div style="height:10px"></div>', unsafe_allow_html=True)
    chips = ["How concentrated am I?", "Do I own the same thing twice?",
             "Should I sell my worst fund?", "What is XIRR?"]
    cols = st.columns(len(chips))
    for col, chip in zip(cols, chips):
        if col.button(chip, key=f"chip_{chip}", use_container_width=True):
            ss.queued = chip
            st.rerun()

    typed = st.chat_input("Ask anything about your portfolio…")
    question = ss.queued or typed
    ss.queued = None
    if question:
        # Paint the question first, answer second: append + rerun so the user's own words
        # are on screen while the tools run, instead of appearing with the answer.
        ss.chat.append(("user", question, {}))
        ss.pending = question
        st.rerun()


# ---------------------------------------------------------------- page: analysis

def page_analysis() -> None:
    portfolio_switcher()
    if not ss.analysis:
        page_header("Portfolio analysis", "Nothing loaded yet.")
        st.markdown('<div class="card"><div class="sec-t">Load a portfolio to see this'
                    '</div><div style="font-size:14px;color:var(--ink2);line-height:1.55;'
                    'margin-top:6px">The sample is six real instruments with a synthetic '
                    'SIP history — enough to show concentration, overlap and exact XIRR.'
                    '</div></div>', unsafe_allow_html=True)
        st.write("")
        c1, c2 = st.columns([1, 3])
        if c1.button("Load the sample", type="primary", use_container_width=True):
            if load_analysis(None, "Sample portfolio", name="Sample"):
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
    pnl_cls = "up" if snap["total_pnl_abs"] >= 0 else "down"
    arrow = "▲" if snap["total_pnl_abs"] >= 0 else "▼"
    k_value = (
        f'<div class="kpi"><div class="k-label">What it\'s worth today</div>'
        f'<div class="k-big">{inr(snap["total_current_value"])}</div>'
        f'<div class="k-note {pnl_cls}">{arrow} {inr(abs(snap["total_pnl_abs"]))} '
        f'<span class="muted">against {inr(snap["total_invested"])} invested</span></div>'
        f'<div style="margin-top:auto;padding-top:16px">'
        f'{sparkline(a.get("timeline") or [], height=44, dark=False)}</div></div>')

    mk = a.get("market") or {}
    xirr = snap.get("portfolio_xirr_pct")
    ref = mk.get("annualised_pct")
    bar_you = min(max((xirr or 0) / 25 * 100, 2), 100)
    bar_mk = min(max((ref or 0) / 25 * 100, 2), 100)
    k_xirr = (
        f'<div class="kpi"><div class="k-label">Your return a year (XIRR)</div>'
        f'<div class="k-num">{pct(xirr)}</div>'
        f'<div class="k-note">NIFTY 50 did {pct(ref)} over the same window</div>'
        f'<div class="duo"><span>You</span><div class="track">'
        f'<div style="width:{bar_you:.0f}%"></div></div>'
        f'<span>NIFTY 50</span><div class="track idx">'
        f'<div style="width:{bar_mk:.0f}%"></div></div></div>'
        f'<div class="k-foot">Yours is money-weighted (your SIP timing); the index '
        f'line is point-to-point.</div></div>')

    effn = div.get("effective_holdings")
    # holdings_count is what the figure is computed OVER, which is not always what you
    # hold: a holding with no price carries no weight, so counting it here would claim a
    # spread across something that never entered the sum.
    total_h = div.get("holdings_count") or a["holdings_total"]
    skipped = div.get("unweighted_unpriced") or 0
    filled = int(round(effn or 0))
    pips = "".join(f'<i class="{"on" if i < filled else ""}"></i>' for i in range(total_h))
    k_spread = (
        f'<div class="kpi"><div class="k-label">How spread out you are</div>'
        f'<div class="k-num">{effn if effn is not None else "—"} '
        f'<span class="u">of {total_h}</span></div>'
        f'<div class="k-note">They behave like '
        f'{effn if effn is not None else "—"} independent bets</div>'
        f'<div class="pips">{pips}</div>'
        f'<div class="k-foot">1 ÷ HHI. Overlapping funds and big positions shrink '
        f'this below your holding count.'
        + (f' {skipped} holding(s) with no price carry no weight and are not counted '
           f'here.' if skipped else "")
        + '</div></div>')

    wc = cost.get("weighted_cost_pct_of_covered")
    covered = len(cost.get("expense_ratios") or [])
    unknown = len(cost.get("ratio_unavailable_for") or [])
    tenyr = cost.get("fees_10yr_est")
    if wc:
        body = (f'<div class="k-num">{wc:.2f}%</div>'
                f'<div class="k-note">≈ {inr(tenyr)} over ten years at this '
                f'rate</div>'
                f'<div class="k-foot">Covers {covered} fund(s) with a disclosed TER'
                + (f'; {unknown} not disclosed on free APIs' if unknown else "")
                + '</div>')
    else:
        body = ('<div class="k-num muted">n/a</div>'
                '<div class="k-note">No fund here discloses its expense ratio '
                'via free APIs</div>'
                '<div class="k-foot">Indian mutual-fund TERs are not machine-readable '
                'without paid data.</div>')
    k_fees = f'<div class="kpi"><div class="k-label">Yearly fees you pay</div>{body}</div>'

    st.markdown(f'<div class="kpis">{k_value}{k_xirr}{k_spread}{k_fees}</div>',
                unsafe_allow_html=True)

    st.write("")

    # --- allocation + worth-a-look --------------------------------------------------
    left, right = st.columns([1, 1])
    with left:
        alloc = sec.get("allocation", {})
        classes = [(str(n), float(v)) for n, v in (alloc.get("asset_classes") or [])]
        geo = [(str(n), float(v)) for n, v in (alloc.get("geography") or [])]
        st.markdown(
            '<div class="card"><div class="sec-head"><div class="sec-t">Where your money '
            'sits</div><div class="sec-s">by asset class</div></div>'
            + stacked(classes) +
            '<div class="hr"></div>'
            '<div class="sec-head">'
            '<div class="sec-t">Which market you\'re exposed to</div>'
            '<div class="sec-s">by geography</div></div>'
            + stacked(geo) +
            '<div class="note">Funds are shown as one line each — free data does not '
            'reveal the individual stocks inside Indian mutual funds, so no market-cap '
            'look-through is claimed here.</div></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="sec-t" style="margin:2px 0 12px">Worth a look</div>',
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
        st.markdown('<div class="ins-list">' + ("".join(cards) or
                    insight("Nothing flagged", "—",
                            "Every computed finding sits inside the comfort lines this "
                            "report uses.", "LOW")) + '</div>', unsafe_allow_html=True)

    st.write("")

    # --- holdings table -------------------------------------------------------------
    st.markdown('<div class="sec-head" style="margin:6px 0 12px"><div class="sec-t">Your '
                f'holdings</div><div class="sec-s">{len(priced)} priced of '
                f'{a["holdings_total"]}</div></div>', unsafe_allow_html=True)
    total_val = snap["total_current_value"] or 1
    rows_html = ['<div class="tbl"><div class="hd"><div>Holding</div><div class="ty">Type</div>'
                 '<div>Share of money</div><div class="r">XIRR / yr</div>'
                 '<div class="r">Value</div></div>']
    for h in sorted(priced, key=lambda x: -(x.get("current_value") or 0)):
        w = (h.get("current_value") or 0) / total_val * 100
        xirr = h.get("xirr_pct")
        xcls = "up" if (xirr or 0) >= 0 else "down"
        rows_html.append(
            f'<div class="tr"><div>{_html.escape(h["name"])}</div>'
            f'<div class="ty">{_html.escape(h.get("type") or "—")}</div>'
            f'<div class="share"><div class="track">'
            f'<div style="width:{min(w * 3, 100):.0f}%"></div></div>'
            f'<span class="v">{w:.1f}%</span>'
            f'</div>'
            f'<div class="r {xcls}">{pct(xirr, signed=True) if xirr is not None else "—"}</div>'
            f'<div class="r">{inr(h.get("current_value"))}</div></div>')
    unavailable = [h for h in snap["holdings"] if h.get("status") != "priced"]
    for h in unavailable:
        rows_html.append(
            f'<div class="tr"><div class="dim">{_html.escape(h["name"])}</div>'
            f'<div class="ty">{_html.escape(h.get("type") or "—")}</div>'
            f'<div class="na">{SLASH_SVG}'
            f'<span>{_html.escape((h.get("reason") or "not priced")[:58])}</span></div>'
            f'<div class="r muted">—</div>'
            f'<div class="r muted">{inr(h.get("invested"))} in</div></div>')
    rows_html.append("</div>")
    st.markdown("".join(rows_html), unsafe_allow_html=True)

    st.write("")
    b1, b2, _ = st.columns([1.1, 1, 2])
    with b1:
        if st.button("Generate health report", type="primary", icon=":material/table_view:",
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
                # Keep header lookups case-insensitive: servers send them lowercased.
                ss.report = (content, requests.structures.CaseInsensitiveDict(headers))
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
        st.download_button("Download Portfolio_Health_Report.xlsx", data=content,
                           file_name="Portfolio_Health_Report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet",
                           icon=":material/download:")
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
        '<div class="card" style="display:flex;gap:12px 26px;align-items:center;'
        'flex-wrap:wrap;padding:16px 20px"><div style="font-size:13px;color:var(--ink3);'
        'max-width:220px;line-height:1.5">What each column is for — only the first and '
        'last are required:</div><div>'
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
        f'<div style="font-size:13px;color:var(--ink3);line-height:1.6;margin-top:8px">'
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
            if load_analysis(csv_text, "Your portfolio (not stored)",
                             name="Typed holdings"):
                st.rerun()
    with c2:
        st.download_button("Blank template", data=csv_text if ready else
                           "Mode,App,Type,Market,Risk,Where,symbol,source,"
                           "Estimated Returns (3Y),Total Invested,Mar'25,April'25\n"
                           "Equity,Groww,Share,India,Med,Reliance Industries,"
                           "RELIANCE.NS,yfinance,-,15000,5000,10000\n",
                           file_name="portfolio_template.csv", mime="text/csv",
                           icon=":material/download:", use_container_width=True)
    with c3:
        if st.button("Cancel", use_container_width=True):
            ss.page = "analysis" if ss.analysis else "ask"
            st.rerun()

    with st.expander("Paste or upload a full CSV instead (unlocks tier L2)"):
        st.markdown('<div style="font-size:13px;color:var(--ink2);line-height:1.6">'
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
                if load_analysis(payload, "Your portfolio (not stored)",
                                 name="Pasted CSV"):
                    st.rerun()


# ---------------------------------------------------------------- render

{"ask": page_ask, "analysis": page_analysis, "holdings": page_holdings}[ss.page]()

code, stats = call_json("GET", "/stats")
if code == 200 and stats.get("queries"):
    st.markdown(
        f'<div class="foot">Live economics from this server\'s own log: '
        f'<span class="mono ink">{stats["queries"]}</span> queries served · avg '
        f'<span class="mono ink">₹{stats["avg_cost_inr"]:.2f}</span> per query · '
        f'<span class="mono ink">{stats.get("reports_generated", 0)}</span> AI reports at '
        f'avg <span class="mono ink">₹{stats.get("avg_report_cost_inr", "—")}</span>. '
        f'Free-demo limits: 20 chat turns and 3 reports per day.</div>',
        unsafe_allow_html=True)
