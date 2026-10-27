# Deploy Runbook — Portfolio X-Ray v1.0

You do the clicking; nothing here was pre-created. Total time ~30 minutes, all free tiers.
Order matters: **GitHub → Render (backend) → Streamlit Cloud (frontend) → smoke test →
UptimeRobot**.

---

## 0. Pre-flight (5 min, local)

1. `uv run pytest test_metrics.py test_agent.py test_report.py -q` → all green.
2. Confirm nothing personal will publish:
   ```powershell
   git status --short          # must be clean
   git check-ignore -v .env portfolio.csv xray.db My_Health_Report.xlsx .streamlit/secrets.toml
   ```
   Every one must print a gitignore rule. **If any prints nothing — stop and tell Claude.**
3. `git log --oneline -3` — the v1.0-candidate commit is at the top.

## 1. Push the repo public (5 min)

1. github.com → **New repository** → name `portfolio-xray`, visibility **Public**,
   no README/gitignore initialisation (we have them).
2. Locally:
   ```powershell
   git remote add origin https://github.com/<YOUR_USER>/portfolio-xray.git
   git push -u origin master
   git push --tags
   ```
3. Refresh GitHub — you should see the README render and the **evals** Action start
   running (it needs no secrets; it must go green).

## 2. Backend on Render (10 min)

1. render.com → sign in with GitHub → **New → Web Service** → pick `portfolio-xray`.
2. Render detects `render.yaml` ("Blueprint"). Accept it. If it asks instead:
   - Runtime **Python 3**, plan **Free**
   - Build command: `pip install uv && uv sync --frozen`
   - Start command: `uvicorn` line is prefilled from render.yaml; otherwise
     `uv run uvicorn api:app --host 0.0.0.0 --port $PORT`
3. Environment variables (Dashboard → Environment):
   | Key | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | paste your key (type it here ONLY — never in the repo) |
   | `XRAY_DISABLED` | `0` |
   | `XRAY_PORTFOLIO` | `portfolio.sample.csv` |
4. **Create Web Service** → wait for "Live" (first build ~5 min).
5. Copy the service URL. Render appends a random suffix when the name is taken, so
   it is **not** simply `portfolio-xray.onrender.com` — this deployment's is
   `https://portfolio-xray-yqdd.onrender.com`. Take it from the Render dashboard rather than
   guessing it, and check `/health` returns `{"status":"ok"}` before moving on.
6. Sanity in the browser: `<URL>/health` → `{"status":"ok","version":"1.0.0"}`.

> **Kill switch:** if costs ever spike, Dashboard → Environment → set `XRAY_DISABLED=1`
> → Save (auto-redeploys in ~1 min). Everything but /health then returns a maintenance
> message.

## 3. Frontend on Streamlit Community Cloud (5 min)

1. share.streamlit.io → sign in with GitHub → **New app**.
2. Repo `portfolio-xray`, branch `master`, main file **`app.py`**.
3. **Advanced settings → Secrets** — paste (this is `.streamlit/secrets.toml.example`
   with your real backend URL):
   ```toml
   BACKEND_URL = "https://portfolio-xray-yqdd.onrender.com"
   ```
4. **Deploy** → you get `https://<something>.streamlit.app` — **this is the URL for the
   Razorpay form.**

## 4. The 4-step smoke test (5 min, in the live UI)

1. **Sample click**: "Try the sample portfolio" → dashboard renders (value ≈ ₹50k,
   XIRR, effective bets, 5/6 priced). First click may show the cold-start note (~30s) —
   that's designed.
2. **Refusal chip**: click "Should I sell my worst fund?" → a refusal with the SEBI line,
   `tools_used` empty.
3. **Report**: "Generate Health Report" → Excel downloads; open it — 11 sheets, charts,
   AI blocks present; cost caption ≈ ₹3–6.
4. **Guardrails over HTTP** (PowerShell, costs a few rupees):
   ```powershell
   $env:XRAY_BASE_URL = "https://portfolio-xray-yqdd.onrender.com"
   uv run pytest tests/test_deployed.py -v
   ```
   All pass = the refusal boundary and the poisoned-name injection defence survived the
   web wrapper. Screenshot this run for the submission.

## 5. Keep-warm ping (3 min)

1. uptimerobot.com → free account → **Add New Monitor**.
2. Type **HTTP(s)**, name `xray-health`, URL `<backend>/health`, interval **5 minutes**.
3. This keeps the free Render instance warm during the review window (it sleeps after
   15 idle minutes otherwise; the frontend's cold-start note covers the rare miss).

## 6. Ship

```powershell
git tag v1.0
git push --tags
```
Then: record the 2-minute fallback video (sample click → refusal chip → report download),
and put the Streamlit URL + repo URL in the Typeform.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/health` 200 but UI says backend unreachable | `BACKEND_URL` secret typo (trailing slash is fine; scheme must be https) |
| First request 30–60s | Free-tier cold start — expected; UptimeRobot minimises it |
| Report button fails with limit message | 3 reports/IP/day — by design; use `XRAY_DISABLED` only for cost spikes, not limits |
| Render build fails on pandas/matplotlib | Re-deploy once (free-tier build memory blip); build is cached the second time |
| Everything on fire during review | Set `XRAY_DISABLED=1`, breathe, fix, set back to `0` |


## If the chat refuses everything

Symptom: every question — including `What is beta?` — comes back with the SEBI refusal,
while the dashboard and report pages work fine.

Cause: the router's classifier call is failing, and it fails **safe**, which means
refusing. An unreachable model therefore looks identical to a compliance decision. The
usual reason is a missing or invalid `ANTHROPIC_API_KEY` on the backend.

Confirm it in one request — `/stats` tells you without spending anything:

```bash
curl -s https://portfolio-xray-yqdd.onrender.com/stats
```

`"avg_cost_usd": 0` together with an average latency around **150ms** means no model call
is being made at all; a real classifier round-trip is ~1s and costs something. Then check
Render → the service → Environment → `ANTHROPIC_API_KEY`, and verify the key still works:

```bash
curl -s https://api.anthropic.com/v1/messages -H "x-api-key: $ANTHROPIC_API_KEY"   -H "anthropic-version: 2023-06-01" -H "content-type: application/json"   -d '{"model":"claude-haiku-5-5","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'
```

Since the fix in this repo, a classifier failure answers with "my query classifier is
unreachable … this is a service problem on my side" instead of the compliance refusal, so
the two are no longer confusable from the outside.
