"""Portfolio X-Ray — Streamlit frontend. Designed for a reviewer with 90 seconds:
one click to a live dashboard, three suggested chat chips (one is deliberately an advice
question — the refusal IS the demo), and a one-button Excel health report.

The backend URL comes from Streamlit secrets (BACKEND_URL) or env; the Anthropic key
lives ONLY on the backend. Run locally: uv run streamlit run app.py
"""
import os
import time

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Portfolio X-Ray", page_icon="🩻", layout="wide")

def _backend_url() -> str:
    # st.secrets RAISES when no secrets.toml exists anywhere (fresh clone / local dev),
    # so the env-var fallback must live behind a try, not a .get() default.
    try:
        return st.secrets["BACKEND_URL"]
    except Exception:
        return os.environ.get("BACKEND_URL", "http://localhost:8000")


BACKEND = _backend_url()
COLD_START_NOTE = ("⏳ Waking the free-tier server (~30s) — worth the wait. "
                   "It sleeps after 15 idle minutes to stay free.")


def call(method: str, path: str, cold_slot=None, **kw):
    """Backend call with cold-start UX: if the first byte takes >3s, tell the reviewer the
    free tier is waking up instead of showing a bare spinner."""
    t0 = time.time()
    slow_shown = False
    kw.setdefault("timeout", 180)
    try:
        with requests.request(method, f"{BACKEND}{path}", stream=True, **kw) as r:
            if time.time() - t0 > 3 and cold_slot is not None and not slow_shown:
                cold_slot.info(COLD_START_NOTE)
                slow_shown = True
            content = r.content  # drain
            return r.status_code, content, r.headers
    except requests.exceptions.RequestException as e:
        return 0, str(e).encode(), {}


def call_json(method: str, path: str, cold_slot=None, **kw):
    code, content, _ = call(method, path, cold_slot, **kw)
    try:
        import json
        return code, json.loads(content)
    except Exception:
        return code, {"error": content.decode(errors="replace")[:300]}


# ---------- persistent banner ----------
st.markdown(
    "<div style='background:#1F3A5F;color:white;padding:10px 16px;border-radius:8px;"
    "font-size:0.9rem'>🛡️ <b>Educational analytics, not investment advice.</b> "
    "Not SEBI-registered. Nothing you enter is stored — uploads are processed and "
    "discarded.</div>", unsafe_allow_html=True)

st.title("🩻 Portfolio X-Ray")
st.caption("An agentic analyst that shows Indian retail investors what their apps don't: "
           "true concentration, fund overlap, exact XIRR — and refuses to give advice.")

if "overview" not in st.session_state:
    st.session_state.overview = None
    st.session_state.overview_label = None
if "chat" not in st.session_state:
    st.session_state.chat = []

# ---------- hero: zero typing to first value ----------
c1, c2 = st.columns([1, 2])
with c1:
    if st.button("▶ Try the sample portfolio", type="primary",
                 use_container_width=True):
        slot = st.empty()
        with st.spinner("Reconstructing units from SIP history…"):
            code, data = call_json("GET", "/overview", cold_slot=slot)
        slot.empty()
        if code == 200:
            st.session_state.overview = data
            st.session_state.overview_label = "Sample portfolio (6 holdings, L2 inputs)"
        else:
            st.error(data.get("error", "Backend unreachable — try again in ~30s."))
with c2:
    with st.expander("…or analyse your own (CSV upload or paste)"):
        st.markdown("**Tier 1 — no amounts needed:** columns `Where, symbol, source, "
                    "Total Invested` are enough (weights-only). Add monthly SIP columns "
                    "(`Mar'25`…) to unlock exact values, risk and cost (Tier 2). "
                    "Max 60 holdings.")
        up = st.file_uploader("Portfolio CSV", type=["csv"], key="csv_up")
        pasted = st.text_area("…or paste CSV text", height=100, key="csv_paste")
        if st.button("Analyse my portfolio"):
            payload = up.getvalue() if up else pasted.encode()
            if not payload or not payload.strip():
                st.warning("Upload or paste a CSV first.")
            else:
                slot = st.empty()
                with st.spinner("Computing…"):
                    code, data = call_json(
                        "POST", "/overview", cold_slot=slot,
                        files={"file": ("portfolio.csv", payload, "text/csv")})
                slot.empty()
                if code == 200:
                    st.session_state.overview = data
                    st.session_state.overview_label = "Your portfolio (not stored)"
                else:
                    st.error(data.get("error", "Could not read that CSV."))

# ---------- dashboard ----------
ov = st.session_state.overview
if ov:
    st.subheader(st.session_state.overview_label)
    snap, conc = ov["snapshot"], ov["concentration"]
    k = st.columns(5)
    k[0].metric("Current value", f"₹{snap['total_current_value']:,.0f}")
    k[1].metric("Invested", f"₹{snap['total_invested']:,.0f}",
                f"{snap['total_pnl_pct']:+.1f}%")
    k[2].metric("XIRR (money-weighted)",
                f"{snap['portfolio_xirr_pct']}%" if snap.get("portfolio_xirr_pct")
                is not None else "n/a")
    k[3].metric("Effective bets (1/HHI)", conc.get("effective_holdings", "n/a"))
    k[4].metric("Priced holdings",
                f"{snap['priced_count']}/{snap['total_count']}")
    left, right = st.columns([3, 2])
    with left:
        rows = [{"Holding": h["name"], "Value ₹": h.get("current_value"),
                 "Invested ₹": h.get("invested"), "P&L %": h.get("pnl_pct"),
                 "XIRR %": h.get("xirr_pct"), "Status": h.get("status")}
                for h in snap["holdings"]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=320)
        st.caption("Unpriced rows are reported, never silently dropped. All figures "
                   "Python-computed (exact unit reconstruction).")
    with right:
        st.markdown("**Concentration (weight by current value)**")
        tw = conc.get("top_weights", [])
        if tw:
            st.bar_chart(pd.DataFrame(tw).set_index("name")["weight_pct"])
        flagged = conc.get("over_threshold", [])
        if flagged:
            st.warning("Over 10% of the book: "
                       + ", ".join(f"{h['name']} ({h['weight_pct']}%)" for h in flagged))

    # ---------- health report ----------
    st.divider()
    r1, r2 = st.columns([1, 2])
    with r1:
        if st.button("📊 Generate Health Report (Excel)", use_container_width=True):
            slot = st.empty()
            prog = st.status("Generating your report…", expanded=True)
            prog.write("Computing metrics (exact units, HHI, overlap, betas)…")
            files = None
            if st.session_state.overview_label and \
                    st.session_state.overview_label.startswith("Your"):
                payload = (st.session_state.csv_up.getvalue()
                           if st.session_state.get("csv_up")
                           else st.session_state.get("csv_paste", "").encode())
                files = {"file": ("portfolio.csv", payload, "text/csv")}
            prog.write("Rendering 11 charts…")
            code, content, headers = call("POST", "/report", cold_slot=slot, files=files)
            if code == 200:
                prog.write("Writing guarded AI insights…")
                prog.update(label="Report ready", state="complete")
                st.download_button(
                    "⬇ Download Portfolio_Health_Report.xlsx", data=content,
                    file_name="Portfolio_Health_Report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet")
                st.caption(f"Input tier {headers.get('X-Report-Level', '?')} · AI insight "
                           f"cost ₹{headers.get('X-Report-Cost-INR', '?')} · limits: 3 "
                           "reports/day")
            else:
                prog.update(label="Report failed", state="error")
                try:
                    import json
                    st.error(json.loads(content).get("answer") or
                             json.loads(content).get("error"))
                except Exception:
                    st.error("Report generation failed — try again shortly.")
    with r2:
        st.markdown("Tiered by YOUR inputs: weights-only unlocks allocation / "
                    "diversification / concentration / overlap; monthly SIP history "
                    "unlocks exact risk & cost. Locked sheets say what unlocks them. "
                    "Every number is Python-computed; the AI may only restate them — "
                    "mechanically enforced.")

# ---------- chat ----------
st.divider()
st.subheader("Ask the analyst")
chips = st.columns(3)
prompts = ["How concentrated am I?", "What's my portfolio beta?",
           "Should I sell my worst fund?"]  # the refusal IS the demo
queued = None
for col, p in zip(chips, prompts):
    if col.button(p, use_container_width=True):
        queued = p
typed = st.chat_input("Ask about value, XIRR, beta, overlap, NAVs, news…")
question = queued or typed

for role, text in st.session_state.chat:
    st.chat_message(role).write(text)

if question:
    st.chat_message("user").write(question)
    history = [{"role": r, "content": t} for r, t in st.session_state.chat]
    slot = st.empty()
    with st.spinner("Routing → tools → answer…"):
        code, data = call_json("POST", "/chat", cold_slot=slot,
                               json={"message": question, "history": history})
    slot.empty()
    answer = data.get("answer", "Backend unreachable — give the free tier ~30s and retry.")
    st.chat_message("assistant").write(answer)
    if data.get("refused"):
        st.caption("↑ That refusal is by design: analytics and education only, "
                   "never buy/sell advice. See the eval table in the README — 100% "
                   "refusal correctness on the adversarial set.")
    elif data.get("tools_used"):
        st.caption("Tools used (all math in Python): " + ", ".join(data["tools_used"]))
    st.session_state.chat.append(("user", question))
    st.session_state.chat.append(("assistant", answer))

# ---------- live economics footer ----------
try:
    code, s = call_json("GET", "/stats")
    if code == 200 and s.get("queries"):
        st.caption(f"Live economics: {s['queries']} queries served · avg "
                   f"₹{s['avg_cost_inr']}/query · {s.get('reports_generated', 0)} AI "
                   f"reports at avg ₹{s.get('avg_report_cost_inr', '—')} each. Chat "
                   "limits: 20/day.")
except Exception:
    pass
