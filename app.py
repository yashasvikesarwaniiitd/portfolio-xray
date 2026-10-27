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


def keep_owner_key_in_browser() -> None:
    """Make the saved portfolios survive closing the tab, not just refreshing it.

    The key lives in the URL (see owner_key), which survives a refresh but not someone
    typing the address again tomorrow - and "I came back and everything was gone" is exactly
    the failure saving exists to prevent. Streamlit has no writable browser storage, so a
    few lines of script do it: remember the key in localStorage, and when the page is opened
    WITHOUT one, put the remembered key back into the URL.

    An existing remembered key is never overwritten by a `?k=` in the URL. Opening a link
    someone sent you should show you their portfolios for that visit, not silently replace
    the key that opens your own.

    The key is embedded in the body so that the element changes - and the script re-runs -
    the moment a first save mints one; otherwise it would run once, before there was
    anything to remember.
    """
    k = owner_key()
    # `k` comes straight from the URL and is about to be written into a script. A link like
    # ?k=';alert(1);' would otherwise run attacker script in the victim's page, so only a
    # well-formed key (32 hex characters, which is all this app ever mints) is embedded.
    if not (len(k) == 32 and all(c in "0123456789abcdef" for c in k)):
        k = ""
    st.html(
        "<script>(function(){try{var S='xray_owner_key',K='" + k + "';"
        "var u=new URL(window.location.href),q=u.searchParams.get('k');"
        "if(q){if(!localStorage.getItem(S)){localStorage.setItem(S,q);}}"
        "else{var s=localStorage.getItem(S);"
        "if(s&&/^[0-9a-f]{32}$/.test(s)){u.searchParams.set('k',s);"
        "window.location.replace(u.toString());}}}catch(e){}})();</script>",
        unsafe_allow_javascript=True)


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
    if ss.get("storage_off"):
        return False, []
    k = owner_key()
    if not k:
        return True, []                      # no key yet: nothing to fetch, nothing to cache
    code, data = call_json("GET", "/portfolios", headers={"X-Owner-Key": k})
    if code != 200 or not isinstance(data, dict):
        # Not cached: a sleeping free-tier backend answers the next request fine, and
        # remembering this failure would hide the saved list for the rest of the session.
        return False, []
    out = (bool(data.get("enabled")), data.get("portfolios") or [])
    ss.saved_cache = out
    return out


def saved_save(name: str, csv_text: str) -> tuple[str | None, str | None]:
    """(portfolio id, error). Both None means storage is simply switched off.

    The owner key is minted here, on the first save, which puts `?k=` in the URL. If the
    server then says saving is off (503) that key was minted for nothing, so it is taken
    back out - a deployment without a database should not leave a meaningless token in
    every uploader's address bar.
    """
    had_key = bool(owner_key())
    code, data = call_json("POST", "/portfolios", headers={"X-Owner-Key": owner_key(True)},
                           json={"name": name, "csv_text": csv_text})
    ss.saved_cache = None
    if code == 200:
        return data.get("id"), None
    if code == 503:
        ss.storage_off = True
        if not had_key:
            st.query_params.pop("k", None)
        return None, None
    return None, (data.get("error") or "Could not save.")


def saved_update(pid: str, csv_text: str | None = None, name: str | None = None) -> str | None:
    """Change a saved portfolio in place. Returns an error message, or None on success."""
    body = {k: v for k, v in (("csv_text", csv_text), ("name", name)) if v is not None}
    code, data = call_json("PUT", f"/portfolios/{pid}", headers={"X-Owner-Key": owner_key()},
                           json=body)
    ss.saved_cache = None
    return None if code == 200 else (data.get("error") or "Could not update the saved copy.")


def merge_remote(existing: str, incoming: str) -> tuple[dict | None, str | None]:
    """Ask the server to fold `incoming` into `existing`. (result, error)."""
    code, data = call_json("POST", "/merge",
                           json={"existing_csv": existing or "", "incoming_csv": incoming})
    if code == 200 and "csv_text" in data:
        return data, None
    return None, (UNREACHABLE if code == 0 else data.get("error", "Could not merge."))


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
/* Upload is the primary way in, so it carries the lime; the sample is the quieter second
   option beneath it, centred. */
[data-testid="stSidebar"] [class*="st-key-rail_upload_btn"] .stButton > button {
  justify-content: center; background: var(--lime); color: var(--lime-ink);
  box-shadow: none; font-weight: 600; min-height: 42px;
}
[data-testid="stSidebar"] [class*="st-key-rail_upload_btn"] .stButton > button div {
  justify-content: center;
}
/* Streamlit colours a button's icon with the theme accent, which is this same lime, so on the
   lime button the icon was lime on lime and simply not there. */
[data-testid="stSidebar"] [class*="st-key-rail_upload_btn"] .stButton > button [data-testid="stIconMaterial"] {
  color: var(--lime-ink);
}
[data-testid="stSidebar"] [class*="st-key-rail_upload_btn"] .stButton > button:hover {
  background: #E2FF7A;
}
[data-testid="stSidebar"] [class*="st-key-rail_sample"] .stButton > button,
[data-testid="stSidebar"] [class*="st-key-rail_sample"] .stButton > button div {
  justify-content: center;
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
  min-height: 36px; border-radius: 999px 0 0 999px; font-size: 13.5px; font-weight: 500;
  border: 1px solid var(--line2); border-right: 0; background: var(--bg1); color: var(--ink2);
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
/* The close segment is fused to the right edge of its pill: the pill's right corners are
   squared above and this one's left corners are squared here, so the pair reads as one
   control with a thin divider rather than two buttons side by side. */
/* Descendant, not child, selector: a button with a `help` tooltip is wrapped in an extra
   element, so `.stButton > button` silently matched nothing and the close segment kept
   Streamlit's default shape. The label-only pill has no tooltip, which is why its rule works. */
.block-container [class*="st-key-pfx_"] .stButton button {
  min-height: 36px; border-radius: 0 999px 999px 0; padding: 0 10px 0 6px;
  border: 1px solid var(--line2); border-left: 1px solid rgba(0,0,0,.22);
  background: var(--bg1); color: var(--ink3);
}
.block-container [class*="st-key-pfx_"] .stButton button:hover {
  background: var(--bg2); color: var(--coral); border-color: var(--ink4);
}
.block-container [class*="st-key-pfx_"] .stButton button[kind="primary"] {
  background: var(--lime); color: var(--lime-ink); border-color: var(--lime);
  border-left: 1px solid rgba(0,0,0,.22);
}
.block-container [class*="st-key-pfx_"] .stButton button[kind="primary"]:hover {
  background: #E2FF7A; color: #B42318;
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
.tbl .share .v { width: 62px; text-align: right; color: var(--ink2);
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
    # Classes from the look-through strip, which have no market in their name. Without
    # these every segment fell through to the same grey and the bar read as one block.
    if n.startswith("equity"):
        return _V_INDIA
    if n.startswith("debt"):
        return _V_US
    if n.startswith("cash"):
        return _V_GLOBAL
    if n.startswith(("fund", "etf")):
        return _V_MF
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
    # Each portfolio is ONE pill with a close segment fused to it, so removing a book is
    # done on the book itself and works with a single portfolio loaded. It used to be a
    # separate "Remove" button that only appeared once there were TWO or more, so a person
    # with one portfolio - the usual case, and the one they most often want to start over
    # from - had no way to remove it at all.
    #
    # Fixed-width groups rather than stretching to the row: two portfolios should not become
    # two half-page pills.
    per_row = 3
    for start in range(0, len(names), per_row):
        chunk = names[start:start + per_row]
        outer = st.columns(per_row)
        for i, (col, name) in enumerate(zip(outer, chunk), start=start):
            with col:
                is_active = name == ss.active
                pill, close = st.columns([6, 1], gap=None)
                with pill:
                    if st.button(name, key=f"pf_{i}", use_container_width=True,
                                 type="primary" if is_active else "secondary"):
                        activate_portfolio(name)
                        st.rerun()
                with close:
                    saved_copy = bool((ss.portfolios.get(name) or {}).get("pid"))
                    if st.button("", key=f"pfx_{i}", use_container_width=True,
                                 icon=":material/close:",
                                 type="primary" if is_active else "secondary",
                                 help=(f"Delete “{name}” and its saved copy" if saved_copy
                                       else f"Remove “{name}”")):
                        if saved_copy:
                            confirm_remove_dialog(name)     # permanent, so ask first
                        else:
                            remove_portfolio(name)
                            st.rerun()


@st.dialog("Delete this portfolio?")
def confirm_remove_dialog(name: str) -> None:
    """Deleting a SAVED portfolio is permanent - there is no undo and the server keeps no
    other copy - so it is confirmed. A session-only one (the sample, or anything with saving
    off) is not: it can be added again in one click."""
    st.markdown(f"**{_html.escape(name)}** and its saved copy will be permanently deleted. "
                "This can't be undone.")
    c1, c2 = st.columns(2)
    if c1.button("Delete", type="primary", use_container_width=True, key="cfm_del"):
        remove_portfolio(name)
        st.rerun()
    if c2.button("Cancel", use_container_width=True, key="cfm_cancel"):
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
ss.setdefault("portfolios", {})      # name -> {csv_text, analysis, label, pid}
ss.setdefault("active", None)
ss.setdefault("rail_upload_sig", None)   # the last file the rail uploader tried
ss.setdefault("saved_cache", None)       # (enabled, rows); None = refetch
ss.setdefault("storage_off", False)      # the server said saving is not switched on
ss.setdefault("restored", False)         # have we already tried to reopen a saved portfolio
ss.setdefault("flash", None)             # (kind, text) to show once after the next rerun
ss.setdefault("save_note", None)         # why the last auto-save did not happen, if it didn't
ss.setdefault("chat", [])
ss.setdefault("queued", None)
ss.setdefault("pending", None)   # question awaiting an answer (already shown in the thread)

def activate_portfolio(name: str) -> None:
    """Point the rest of the app at one of the loaded portfolios.

    Everything downstream still reads ss.analysis / ss.csv_text, so switching costs
    nothing and refetches nothing — the analysis was already paid for when it loaded.

    The chat is cleared on purpose. A thread is about one book: carrying "your biggest
    position is Reliance at 26%" over to a portfolio that does not hold Reliance would
    put stale figures under a new heading, and the model would answer follow-ups from
    them. A health report is NOT dropped on a switch: it belongs to its portfolio (see
    `entry["report"]`), so switching away and back finds it where it was.
    """
    entry = ss.portfolios.get(name)
    if not entry:
        return
    ss.active = name
    ss.analysis = entry["analysis"]
    ss.csv_text = entry["csv_text"]
    ss.source_label = entry["label"]
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


def load_analysis(csv_text: str | None, label: str, name: str | None = None,
                  replace: str | None = None, pid: str | None = None) -> bool:
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
        # `replace` names an EXISTING portfolio to overwrite in place (an update after
        # merging a file in), so it keeps its slot in the switcher and its saved id rather
        # than becoming "Name (2)" next to a stale copy of itself.
        key = replace if replace in ss.portfolios else unique_name(name or label)
        keep_pid = (ss.portfolios.get(key) or {}).get("pid")
        ss.portfolios[key] = {"csv_text": csv_text, "analysis": data, "label": label,
                              "pid": pid or keep_pid}
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


def read_upload(f) -> tuple[str | None, str | None, str | None]:
    """(portfolio text, note, error) for an uploaded CSV or Excel file.

    An Excel workbook is converted by the server into the same text a CSV export would be,
    so everything after this point - merging, saving, analysis, the chat - is unchanged and
    does not know the difference. The conversion happens server-side because the frontend
    must not import backend modules, and it is cached per file: this runs on every rerun of
    the dialog, and re-uploading a workbook to convert it again on each click would be a
    network round trip per keystroke in the name box.

    A failure to reach the server is not cached, so the next rerun retries it.
    """
    if not f.name.lower().endswith((".xlsx", ".xlsm")):
        return f.getvalue().decode("utf-8-sig", "replace"), None, None
    sig = (f.name, f.size)
    cache = ss.get("xl_cache") or {}
    if sig in cache:
        return cache[sig]
    with st.spinner("Reading the workbook…"):
        code, data = call_json("POST", "/convert",
                               files={"file": (f.name, f.getvalue(),
                                               "application/octet-stream")})
    if code == 200 and "csv_text" in data:
        out = (data["csv_text"], data.get("summary"), None)
    else:
        out = (None, None, UNREACHABLE if code == 0 else
               data.get("error", "Could not read that workbook."))
    if code != 0:
        ss.xl_cache = {**cache, sig: out}
    return out


def persist(key: str) -> None:
    """Keep the portfolio called `key` in the database, so a refresh does not lose it.

    This runs automatically on every load and every update. It used to be a "Save this
    portfolio" button the reader had to remember to press, and nobody did - the portfolio
    simply vanished on refresh and the database sat empty, which looked like storage not
    working when it was storage never being asked.

    Failure is never fatal and never loud: the portfolio stays usable for this session and
    a one-line note says it was not kept. An update whose saved row has gone (expired, or
    deleted from another tab) falls back to saving a fresh one.
    """
    entry = ss.portfolios.get(key)
    if not entry or not entry["csv_text"] or ss.storage_off:
        return
    if entry.get("pid"):
        if saved_update(entry["pid"], csv_text=entry["csv_text"], name=key) is None:
            ss.save_note = None
            return
        entry["pid"] = None
    pid, err = saved_save(key, entry["csv_text"])
    if pid:
        entry["pid"] = pid
        ss.save_note = None
    elif err:
        ss.save_note = err


def forget_portfolio(key: str) -> None:
    """Remove a portfolio, including its saved copy - otherwise it would come straight back
    on the next refresh, which is the opposite of what pressing Remove means."""
    entry = ss.portfolios.pop(key, None)
    if entry and entry.get("pid"):
        saved_delete(entry["pid"])


def remove_portfolio(key: str) -> None:
    """Remove a portfolio and leave the app pointing at something valid.

    Removing the one you are looking at must move to another portfolio, or - if it was the
    only one - back to the empty state, with nothing left behind: a stale analysis, chat
    thread or report from a portfolio that no longer exists would sit under the next page
    as if it still described something.
    """
    forget_portfolio(key)
    if ss.active == key or ss.active not in ss.portfolios:
        if ss.portfolios:
            activate_portfolio(next(iter(ss.portfolios)))
        else:
            ss.active = None
            ss.analysis = None
            ss.csv_text = None
            ss.source_label = None
            ss.chat = []


def open_saved(row: dict) -> bool:
    """Open a saved portfolio. One already open in this session is just switched to."""
    for key, entry in ss.portfolios.items():
        if entry.get("pid") == row["id"]:
            activate_portfolio(key)
            ss.page = "analysis"
            return True
    got = saved_load(row["id"])
    if not got:
        st.error("That portfolio could not be loaded.")
        return False
    return load_analysis(got["csv_text"], "Saved portfolio", name=got["name"], pid=got["id"])


SIP_TEMPLATE = (
    "Mode,App,Type,Market,Risk,Where,symbol,source,Estimated Returns (3Y),Total Invested,"
    "Mar'25,April'25\n"
    "Equity,Groww,Share,India,Med,Reliance Industries,RELIANCE.NS,yfinance,-,15000,"
    "5000,10000\n")


# ---------------------------------------------------------------- the upload dialog

@st.dialog("Add a portfolio file", width="medium")
def upload_dialog() -> None:
    """One file, two intentions: extend the portfolio you are on, or start a new one.

    UPDATE merges the file into the open portfolio, recognising rows it already holds, so a
    statement exported twice - or two exports whose dates overlap - changes nothing the
    second time rather than doubling every position. The server computes the merge and the
    dialog shows what it WOULD do before anything is applied, which is the only way a
    person can tell "added 12 trades" from "silently did nothing".

    NEW creates a separate portfolio under a name the person chooses. It used to take the
    file's name, which for a Groww export is something like "Stocks_Order_History_2024-
    25_xyz" and not something anyone wants in a switcher.

    The sample cannot be updated: it is the server's own file, not text this browser holds.
    """
    active = ss.active
    can_update = bool(active and (ss.portfolios.get(active) or {}).get("csv_text"))
    modes = ([f"Update “{active}”"] if can_update else []) + ["Add as a new portfolio"]
    mode = st.radio("What should this file do?", modes, key=f"dlg_mode_{active}_{can_update}")
    updating = can_update and mode == modes[0]
    if updating:
        st.caption("Adds the new trades or months to this portfolio. Anything already in it "
                   "is recognised and skipped, so uploading the same file again changes "
                   "nothing. Stocks and mutual funds can live in the same portfolio.")
    else:
        st.caption("Keeps your current portfolio as it is and creates a separate one you can "
                   "switch to from the top of the page."
                   + (" The sample cannot be added to, so it is always a new portfolio."
                      if active and not can_update else ""))

    f = st.file_uploader("CSV or Excel file", type=["csv", "xlsx", "xlsm"], key="dlg_file")
    if f is None:
        ss.dlg_sig = None
        st.caption("A Groww equity or mutual-fund transaction report, or the SIP sheet, as "
                   "CSV or Excel (.xlsx). An Excel workbook can have several sheets; every "
                   "one that looks like a portfolio is read.")
        # The blank template used to live on the "Enter my holdings" page. That page is gone
        # - uploading covers it - but someone building a file by hand still needs the
        # columns, so the template is offered here, where the file is chosen.
        st.download_button("Download a blank SIP template", data=SIP_TEMPLATE,
                           file_name="portfolio_template.csv", mime="text/csv",
                           icon=":material/download:", key="dlg_template")
        return
    text, note, err = read_upload(f)
    if err:
        st.error(err)
        return
    if note:
        st.caption(note)

    if updating:
        sig = (f.name, f.size, active, len(ss.portfolios[active]["csv_text"] or ""))
        cached = ss.get("dlg_merge")
        if not cached or cached[0] != sig:
            with st.spinner("Checking what is new in this file…"):
                res, err = merge_remote(ss.portfolios[active]["csv_text"], text)
            ss.dlg_merge = cached = (sig, res, err)
        _, res, err = cached
        if err:
            st.error(err)
            return
        report = res["report"]
        (st.success if report["changed"] else st.info)(res["summary"])
        for n in report.get("notes") or []:
            st.caption(n)
        if st.button("Update portfolio", type="primary", use_container_width=True,
                     disabled=not report["changed"], key="dlg_go_update"):
            if load_analysis(res["csv_text"], ss.portfolios[active]["label"], replace=active):
                persist(active)
                ss.flash = ("success", f"“{active}” updated. {res['summary']}")
                ss.dlg_merge = None
                st.rerun()
        return

    stem = os.path.splitext(f.name)[0][:40] or "Portfolio"
    sig = (f.name, f.size)
    if ss.get("dlg_sig") != sig:
        # Pre-fill from the file name once per file, so editing the name is not undone by
        # the next rerun. Set before the widget exists in this run, which is what makes
        # assigning to a widget's key legal.
        ss.dlg_sig = sig
        ss.dlg_name = stem
    name = st.text_input("Portfolio name", key="dlg_name", max_chars=40,
                         help="Shown in the switcher at the top of every page.")
    clean = " ".join(name.split())
    final = unique_name(clean) if clean else ""
    if clean and final != clean:
        st.caption(f"You already have a portfolio called “{clean}”, so this one will be "
                   f"named “{final}”.")
    if st.button("Add portfolio", type="primary", use_container_width=True,
                 disabled=not clean, key="dlg_go_new"):
        if load_analysis(text, "Your portfolio", name=clean):
            persist(ss.active)
            ss.flash = ("success", f"“{ss.active}” added.")
            st.rerun()


# ---------------------------------------------------------------- restore on refresh

keep_owner_key_in_browser()
if not ss.restored:
    # Once per session. A refresh wipes session state, so without this the saved portfolio
    # sat in the database while the page showed "nothing loaded" - storage working, and
    # indistinguishable from storage broken. The most recently used one reopens; the rest
    # are one click away in the Saved list.
    ss.restored = True
    if owner_key() and not ss.portfolios:
        _ok, _rows = saved_list()
        if _rows:
            open_saved(_rows[0])
        elif not _ok:
            ss.flash = ("warning", "Your saved portfolios could not be reached just now "
                                   "(the server may be waking up). Reload in a moment.")


# ---------------------------------------------------------------- the dark rail

with st.sidebar:
    st.markdown(f'<div class="rail-brand">{MARK_SVG}<span class="wm"><b>Portfolio</b>'
                f'<i>X-Ray</i></span></div>'
                '<div class="rail-sub">A clearer look at what you own</div>',
                unsafe_allow_html=True)
    st.write("")
    nav = [("ask", "Ask the analyst", ":material/forum:"),
           ("analysis", "Portfolio analysis", ":material/space_dashboard:"),
           ("report", "Health report", ":material/summarize:")]
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
            f'{ss.analysis["holdings_total"]} holdings priced'
            f'{" · " + str(ss.analysis["holdings_closed"]) + " sold" if ss.analysis.get("holdings_closed") else ""}'
            f' · input tier '
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

    # Upload opens a dialog rather than showing a bare dropzone, because "add this file"
    # is two different intentions: extend the portfolio you are looking at, or start a new
    # one. A dropzone cannot ask which, and guessing wrong either duplicates a portfolio or
    # overwrites one.
    if st.button("Upload", key="rail_upload_btn", use_container_width=True,
                 icon=":material/upload:", type="primary"):
        upload_dialog()

    if "Sample" not in ss.portfolios:
        if st.button("Load the sample", key="rail_sample", use_container_width=True):
            if load_analysis(None, "Sample portfolio", name="Sample"):
                st.rerun()
    st.markdown('<div class="rail-cap">A Groww equity or mutual-fund transaction report, '
                'or the SIP sheet — CSV or Excel.</div>', unsafe_allow_html=True)

    # --- saved portfolios -----------------------------------------------------------
    # Shown only once saving is known to work - a key exists, or something has been saved.
    # A deployment with no database never shows it, rather than promising something it
    # cannot do and then quietly not doing it.
    enabled, saved = saved_list()
    has_saved_copy = any(e.get("pid") for e in ss.portfolios.values())
    if enabled and not ss.storage_off and (owner_key() or has_saved_copy):
        st.markdown('<div class="rail-hr"></div>', unsafe_allow_html=True)
        st.markdown('<div class="rail-label">Saved</div>', unsafe_allow_html=True)

        open_pids = {e.get("pid"): k for k, e in ss.portfolios.items() if e.get("pid")}
        for row in saved:
            c1, c2 = st.columns([4, 1])
            with c1:
                is_active = open_pids.get(row["id"]) == ss.active and ss.active is not None
                if st.button(row["name"], key=f"sv_{row['id']}", use_container_width=True,
                             icon=":material/folder_open:",
                             type="primary" if is_active else "secondary",
                             help=f"Saved {row['created_at'][:10]}"):
                    if open_saved(row):
                        st.rerun()
            with c2:
                if st.button("", key=f"rm_{row['id']}", use_container_width=True,
                             icon=":material/delete:", help="Delete permanently"):
                    loaded = next((k for k, e in ss.portfolios.items()
                                   if e.get("pid") == row["id"]), None)
                    if loaded:
                        remove_portfolio(loaded)
                    else:
                        saved_delete(row["id"])
                    st.rerun()

        if ss.save_note:
            st.markdown(f'<div class="rail-cap">Not saved: {_html.escape(ss.save_note)} '
                        'It stays open for this session.</div>', unsafe_allow_html=True)
        elif saved:
            # The URL IS the key. Saying so is the only honest way to offer this without
            # accounts: there is no password to steal and no way to recover a lost link.
            st.markdown('<div class="rail-cap">Saved automatically. This browser remembers '
                        'the link that brings them back; anyone you send it to can see '
                        f'them. Kept {STORAGE_TTL_DAYS} days.</div>', unsafe_allow_html=True)

    st.markdown('<div class="rail-foot">Educational only — not advice, not '
                'SEBI-registered. A portfolio you add is saved so it is still here after a '
                'refresh (only if saving is switched on for this server); delete it any '
                'time. The sample is never stored.</div>',
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
        if not ss.analysis:
            # Nothing is loaded, so say so. Otherwise the backend answers about ITS sample
            # book as if it were the reader's own.
            payload["no_portfolio"] = True
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

_FUND_NOISE = {"fund", "direct", "growth", "plan", "option", "regular", "idcw", "-", "–"}


def _short_fund(name: str) -> str:
    """"Parag Parikh Flexi Cap Fund - Direct Growth" -> "Parag Parikh Flexi". Enough to tell
    funds apart in a narrow column without the plan and option words every name ends with."""
    words = [w for w in str(name or "").split() if w.lower() not in _FUND_NOISE]
    return " ".join(words[:3]) or str(name or "")


def forms_text(e: dict) -> str:
    """How one exposure is held: "Direct 20.00% · Parag Parikh Flexi 3.10%"."""
    return " · ".join(
        f'{"Direct" if f["form"] == "Direct" else _short_fund(f["via"] or f["form"])} '
        f'{f["pct_of_portfolio"]:.2f}%' for f in e["forms"][:4]
    ) + (f' · +{len(e["forms"]) - 4} more' if len(e["forms"]) > 4 else "")


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
        c1, _ = st.columns([1, 3])
        if c1.button("Load the sample", type="primary", use_container_width=True):
            if load_analysis(None, "Sample portfolio", name="Sample"):
                st.rerun()
        st.caption("Or use Upload in the left sidebar to add your own CSV or Excel file.")
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
    # The headline is UNREALISED: what the positions you still hold are worth against what
    # they cost. Profit already banked by selling is a different number, so it is shown on its
    # own line beside it rather than folded in (which would hide it) or left out (which made a
    # book that had booked gains look worse than it was).
    realised = snap.get("total_realised_pnl") or 0
    real_line = ""
    if realised:
        total = snap.get("total_pnl_incl_realised", snap["total_pnl_abs"] + realised)
        real_line = (
            f'<div class="k-foot" style="margin-top:8px">'
            f'{"+" if realised >= 0 else "−"}{inr(abs(realised))} already realised from '
            f'sales · <span class="{"up" if total >= 0 else "down"}">'
            f'{"+" if total >= 0 else "−"}{inr(abs(total))} in total</span></div>')
    k_value = (
        f'<div class="kpi"><div class="k-label">What it\'s worth today</div>'
        f'<div class="k-big">{inr(snap["total_current_value"])}</div>'
        f'<div class="k-note {pnl_cls}">{arrow} {inr(abs(snap["total_pnl_abs"]))} '
        f'<span class="muted">unrealised, on {inr(snap["total_invested"])} you still '
        f'hold</span></div>{real_line}'
        f'<div style="margin-top:auto;padding-top:16px">'
        f'{sparkline(a.get("timeline") or [], height=44, dark=False)}</div></div>')

    mk = a.get("market") or {}
    xirr = snap.get("portfolio_xirr_pct")
    # Like-for-like when the server could compute it: the SAME purchases and sales replayed
    # into NIFTY 50. The point-to-point move since the first purchase is only a fallback - it
    # is not comparable with a money-weighted return when most of the money went in recently.
    same = mk.get("same_money_xirr_pct")
    ref = same if same is not None else mk.get("annualised_pct")
    bar_you = min(max((xirr or 0) / 25 * 100, 2), 100)
    bar_mk = min(max((ref or 0) / 25 * 100, 2), 100)
    k_xirr = (
        f'<div class="kpi"><div class="k-label">Your return a year (XIRR)</div>'
        f'<div class="k-num">{pct(xirr)}</div>'
        f'<div class="k-note">{"Your same money in NIFTY 50: " + pct(ref) + " a year" if same is not None else "NIFTY 50 did " + pct(ref) + " over the same window"}</div>'
        f'<div class="duo"><span>You</span><div class="track">'
        f'<div style="width:{bar_you:.0f}%"></div></div>'
        f'<span>NIFTY 50</span><div class="track idx">'
        f'<div style="width:{bar_mk:.0f}%"></div></div></div>'
        + (f'<div class="k-foot">Both are money-weighted: the index line puts your own '
           f'purchases and sales, on their own dates, into NIFTY 50 instead.</div></div>'
           if same is not None else
           f'<div class="k-foot">Yours is money-weighted (your SIP timing); the index '
           f'line is point-to-point.</div></div>'))

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
            '<div class="note">Funds are shown as one line each here. The stocks inside '
            'them are broken out under "What you actually own" below, for the funds whose '
            'full SEBI monthly disclosure is in the snapshot; no market-cap look-through '
            'is claimed on this chart.</div></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="sec-t" style="margin:2px 0 12px">Worth a look</div>',
                    unsafe_allow_html=True)
        cards = []
        if ov.get("pairs"):
            p = ov["pairs"][0]
            full = p.get("basis") == "full_holdings_isin"
            # The two bases are different scales, so the wording and the threshold both
            # have to follow the basis. 40% of ten NAMES is ordinary; 25% of a fund's
            # VALUE being the same securities is not.
            tone = ("HIGH" if p["overlap_pct"] >= (25 if full else 40) else "MEDIUM")
            if full:
                shared = ", ".join((p.get("shared_top") or [])[:3])
                body = (f'{p["pair"]} hold that share of their value in the same '
                        f'securities — {p.get("common_count", 0)} companies in common'
                        + (f', led by {shared}' if shared else '') + '. Two fee lines, '
                        f'largely one bet. Full SEBI monthly disclosure, as of '
                        f'{p.get("as_of", "—")}.')
            else:
                body = (f'{p["pair"]} share that share of their disclosed top-10 '
                        f'holdings. Two fee lines, largely one bet. (Top-10 basis, so '
                        f'the true figure is likely higher.)')
            cards.append(insight("Two funds own much the same thing",
                                 f'{p["overlap_pct"]:.0f}%', body, tone))
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
                f'{snap["total_count"] - snap["priced_count"] - snap.get("closed_count", 0)} '
                f'holding(s) have no '
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

    # --- what you actually own ---------------------------------------------------------
    # A holding's NAME says nothing about exposure. HDFC Bank held as a share and also inside
    # a fund is one exposure held two ways, so this lists each company once with every form
    # it is held in. The class strip above it covers the non-stock sleeves - debt, cash, gold
    # and silver, crypto - so the whole portfolio is accounted for. Shown only when there is
    # something to add: a book of nothing but direct shares would just repeat the table below.
    lt = sec.get("lookthrough") or {}
    expo = lt.get("exposures") or []
    worth_showing = expo and (lt.get("funds_seen_through")
                              or any(e["held_via"] > 1 for e in expo)
                              or len(lt.get("by_class") or []) > 1)
    if worth_showing:
        as_of = ", ".join(lt.get("as_of") or [])
        st.markdown(
            '<div class="sec-head" style="margin:6px 0 12px"><div class="sec-t">What you '
            'actually own</div><div class="sec-s">every way you hold it, added up'
            + (f' · fund data as of {_html.escape(as_of)}' if as_of else '')
            + '</div></div>', unsafe_allow_html=True)

        classes = [(c["asset_class"], float(c["pct"])) for c in lt["by_class"]
                   if c["pct"] > 0]
        if classes:
            st.markdown('<div class="card">' + stacked(classes) + '</div>',
                        unsafe_allow_html=True)
            st.write("")

        top = lt.get("top_exposures") or []
        if top:
            lt_rows = ['<div class="tbl"><div class="hd"><div>Company</div>'
                       '<div class="ty">Forms</div><div>Share of portfolio</div>'
                       '<div class="r">How you hold it</div></div>']
            for e in top[:12]:
                w = e["total_pct"]
                lt_rows.append(
                    f'<div class="tr"><div>{_html.escape(e["name"])}</div>'
                    f'<div class="ty">{e["held_via"]}</div>'
                    f'<div class="share"><div class="track">'
                    f'<div style="width:{min(w * 8, 100):.0f}%"></div></div>'
                    f'<span class="v">{w:.2f}%</span></div>'
                    f'<div class="r dim">{_html.escape(forms_text(e))}</div></div>')
            lt_rows.append("</div>")
            st.markdown("".join(lt_rows), unsafe_allow_html=True)

        st.markdown(f'<div class="note">{_html.escape(lt["note"])}</div>',
                    unsafe_allow_html=True)
        with st.expander(f"Search all {len(expo)} lines"
                         + (f" (of {lt['rows_total']})" if lt["rows_total"] > len(expo) else "")):
            df = pd.DataFrame([{
                "Name": e["name"], "Class": e["asset_class"],
                "Total %": e["total_pct"], "Direct %": e["direct_pct"],
                "Through funds %": e["via_funds_pct"], "How you hold it": forms_text(e),
            } for e in expo])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         column_config={
                             "Total %": st.column_config.NumberColumn(format="%.2f"),
                             "Direct %": st.column_config.NumberColumn(format="%.2f"),
                             "Through funds %": st.column_config.NumberColumn(format="%.2f"),
                         })
        st.write("")

    # --- how the funds were matched ---------------------------------------------------
    # A fund statement names a scheme; pricing it needs an AMFI scheme code, which is a
    # judgement made on the reader's behalf. Choosing the wrong scheme priced two funds on
    # index funds and turned a roughly flat book into a Rs 1 lakh "loss" without any sign
    # that anything was off. So the match, and whether it was checked against the NAVs the
    # statement itself recorded, is shown rather than assumed.
    matches = [n for n in (a.get("skipped_rows") or [])
               if "AMFI" in n or "not pricing it" in n or "could not resolve" in n]
    if matches:
        checked = sum(1 for n in matches if "[verified:" in n)
        with st.expander(f"How your funds were matched ({checked} of {len(matches)} "
                         "verified against your statement)"):
            for n in matches:
                ok = "[verified:" in n
                st.markdown(
                    f'<div style="font-size:13px;line-height:1.55;color:var(--ink2);'
                    f'margin:0 0 8px">{"✓" if ok else "⚠"} {_html.escape(n)}</div>',
                    unsafe_allow_html=True)
            st.caption("Each fund is priced only against an AMFI scheme whose NAVs match the "
                       "purchases on your statement. One that does not match is left unpriced "
                       "rather than guessed.")
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
                # Kept ON THE PORTFOLIO it was built for, so the Health report tab can list
                # every report built this session and switching portfolios does not lose
                # one. Header lookups are case-insensitive: servers send them lowercased.
                h = requests.structures.CaseInsensitiveDict(headers)
                entry = ss.portfolios.get(ss.active)
                if entry is not None:
                    entry["report"] = {
                        "content": content, "level": h.get("X-Report-Level", "?"),
                        "cost": h.get("X-Report-Cost-INR", "?"),
                        "built_at": time.strftime("%d %b, %H:%M")}
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
    rep = (ss.portfolios.get(ss.active) or {}).get("report")
    if rep:
        d1, d2, _ = st.columns([1.3, 1.1, 1.6])
        with d1:
            st.download_button("Download Portfolio_Health_Report.xlsx", data=rep["content"],
                               file_name=_report_filename(ss.active),
                               mime=XLSX_MIME, icon=":material/download:",
                               key="dl_analysis")
        with d2:
            if st.button("View in Health report", icon=":material/summarize:",
                         use_container_width=True, key="goto_report"):
                ss.page = "report"
                st.rerun()
        st.caption(f"Input tier {rep['level']} · AI insight cost ₹{rep['cost']} · every "
                   f"insight is checked: no buy/sell language, and every number must exist "
                   f"in the computed section data.")


# ---------------------------------------------------------------- page: health report

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _report_filename(name: str | None) -> str:
    """One file per portfolio, named for it - two downloads must not both be called
    Portfolio_Health_Report.xlsx and overwrite each other in the Downloads folder."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("_") or "Portfolio"
    return f"{safe}_Health_Report.xlsx"


def page_report() -> None:
    """Every health report built this session, one card per portfolio.

    The report is an Excel file, which used to make it opaque: a download button and a
    caption, and no way to see what it said without saving it and opening Excel. Each card
    now shows the report's own executive summary - read back out of the workbook, which is
    the only place those findings exist - beside the figures the dashboard already holds, with
    the download beside them.

    It populates only from reports that have actually been built; nothing is generated here,
    because building one takes a server round trip and, with the AI narration, a few rupees.
    Reports live in this browser session, like the portfolios they belong to, and are NOT
    saved to the database: each holds an entire portfolio's contents and the findings are
    cheap to regenerate. A portfolio that is updated loses its report, since it would now
    describe a portfolio that no longer exists.
    """
    import report_view

    page_header("Health report",
                "The Excel reports you have built, one per portfolio, and what each found.")
    built = [(k, e) for k, e in ss.portfolios.items() if e.get("report")]
    waiting = [k for k, e in ss.portfolios.items() if not e.get("report")]

    if not built:
        st.markdown(
            '<div class="card"><div class="sec-t">No health report yet</div>'
            '<div style="font-size:14px;color:var(--ink2);line-height:1.6;margin-top:6px">'
            'A health report is a multi-sheet Excel workbook: allocation, diversification, '
            'concentration, sector exposure, fund overlap, risk, cost and a ranked list of '
            'questions to think about, each with a plain-English reading guide. Build one '
            'from <b>Portfolio analysis</b> and it will appear here with its findings and '
            'the download.</div></div>', unsafe_allow_html=True)
        st.write("")
        if ss.analysis:
            if st.button("Go to Portfolio analysis", type="primary", key="rep_go_analysis"):
                ss.page = "analysis"
                st.rerun()
        elif st.button("Load the sample", type="primary", key="rep_load_sample"):
            if load_analysis(None, "Sample portfolio", name="Sample"):
                st.rerun()
        return

    for i, (key, entry) in enumerate(built):
        rep = entry["report"]
        a = entry["analysis"]
        div = (a.get("sections") or {}).get("diversification") or {}
        score = report_view.score(rep["content"])
        findings = report_view.findings(rep["content"])
        sheets = report_view.sections(rep["content"])

        stats = [("Holdings", a.get("holdings_total", "—")),
                 ("Effective bets", div.get("effective_holdings", "—")),
                 ("Score", f"{score:.0f}/100" if score is not None else "—"),
                 ("Input tier", rep["level"])]
        chips = "".join(
            f'<div style="min-width:96px"><div class="k-label">{_html.escape(str(k))}</div>'
            f'<div class="mono" style="font-size:20px;color:var(--ink1);margin-top:2px">'
            f'{_html.escape(str(v))}</div></div>' for k, v in stats)
        bullets = "".join(
            f'<li style="margin:0 0 8px">{_html.escape(t)}</li>' for t in findings) or (
            '<li class="muted">This report has no written summary.</li>')
        st.markdown(
            f'<div class="card" style="margin-bottom:6px">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
            f'gap:12px;flex-wrap:wrap"><div class="sec-t">{_html.escape(key)}</div>'
            f'<div class="sec-s">built {_html.escape(rep["built_at"])} · AI insight cost '
            f'₹{_html.escape(str(rep["cost"]))}</div></div>'
            f'<div style="display:flex;gap:28px;flex-wrap:wrap;margin:14px 0 4px">{chips}</div>'
            f'<div class="rail-hr" style="margin:14px 0"></div>'
            f'<div class="k-label">What it found</div>'
            f'<ul style="font-size:14px;color:var(--ink2);line-height:1.6;margin:8px 0 0;'
            f'padding-left:20px">{bullets}</ul>'
            f'<div class="note" style="margin-top:14px">{len(sheets)} sheets: '
            f'{_html.escape(" · ".join(sheets))}</div></div>', unsafe_allow_html=True)
        c1, c2, _ = st.columns([1.5, 1.1, 2])
        with c1:
            st.download_button("Download Excel report", data=rep["content"],
                               file_name=_report_filename(key), mime=XLSX_MIME,
                               icon=":material/download:", key=f"dl_report_{i}",
                               use_container_width=True)
        with c2:
            if st.button("Open portfolio", key=f"open_report_{i}", use_container_width=True,
                         icon=":material/space_dashboard:"):
                activate_portfolio(key)
                ss.page = "analysis"
                st.rerun()
        st.write("")

    if waiting:
        st.markdown('<div class="sec-t" style="margin:8px 0 8px">Not built yet</div>',
                    unsafe_allow_html=True)
        for j, key in enumerate(waiting):
            c1, c2 = st.columns([3, 1.4])
            c1.markdown(f'<div style="padding-top:8px;color:var(--ink2)">'
                        f'{_html.escape(key)}</div>', unsafe_allow_html=True)
            if c2.button("Build report", key=f"build_report_{j}", use_container_width=True):
                activate_portfolio(key)
                ss.page = "analysis"
                st.rerun()

    st.caption("Reports are kept for this browser session only and are not saved with your "
               "portfolios. Updating a portfolio removes its report, since it would no longer "
               "describe it.")


# ---------------------------------------------------------------- render

if ss.flash:
    # Shown once, on the run AFTER the action: st.success before st.rerun() is wiped by the
    # rerun, so the confirmation has to be carried across it in session state.
    _kind, _text = ss.flash
    ss.flash = None
    {"success": st.success, "warning": st.warning}.get(_kind, st.info)(_text)

{"ask": page_ask, "analysis": page_analysis, "report": page_report}.get(ss.page, page_ask)()
