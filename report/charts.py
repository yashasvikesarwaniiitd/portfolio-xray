"""Report charts — ported from reference/charts_render.py, parameterized to real data.

Every design decision is kept from the approved reference: style constants, value labels on
bars, reference lines (50% Pareto, beta=1.0, 30% sector comfort, 20/40% overlap thresholds,
0.3% index-fund zone), traffic-light colouring, no legends except the radar (bottom).
Each function takes its data + an output dir and returns the PNG path, or None when the data
is empty/insufficient — the builder then skips the image and keeps the section honest.
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

NAVY = "#1F3A5F"; TEAL = "#2E8B8B"; GOLD = "#C9962A"; SLATE = "#64748B"
RED = "#C0392B"; GREEN = "#1E8449"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.edgecolor": "#D5DBE1",
    "axes.labelcolor": NAVY, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.titlecolor": NAVY, "xtick.color": "#22303C", "ytick.color": "#22303C",
    "figure.facecolor": "white", "axes.facecolor": "white"})

PALETTE = [TEAL, NAVY, GOLD, "#7FB3B3", "#9AA8BD", "#D9C08A", "#B9C7C7"]


def _save(fig, outdir: str, name: str) -> str:
    path = os.path.join(outdir, f"{name}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _declutter(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def render_gauge(score: float, outdir: str):
    """1. Diversification-score donut gauge."""
    if score is None:
        return None
    fig, ax = plt.subplots(figsize=(4.2, 2.6), subplot_kw={"aspect": "equal"})
    ax.pie([score, 100 - score], startangle=90, counterclock=False,
           colors=[TEAL, "#E7ECF0"], wedgeprops=dict(width=0.32))
    ax.text(0, 0.05, f"{score:.0f}", ha="center", va="center", fontsize=30,
            fontweight="bold", color=NAVY)
    ax.text(0, -0.28, "/100", ha="center", va="center", fontsize=11, color=SLATE)
    ax.set_title("Diversification score")
    return _save(fig, outdir, "gauge")


def render_alloc(pairs: list, outdir: str):
    """2. Asset-class donut. pairs = [(label, pct), ...]"""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    vals = [v for _, v in pairs]
    labs = [f"{n}\n{v:.0f}%" for n, v in pairs]
    ax.pie(vals, labels=labs, colors=PALETTE, startangle=90, counterclock=False,
           wedgeprops=dict(width=0.42), textprops={"fontsize": 9.5})
    ax.set_title("By asset class")
    return _save(fig, outdir, "alloc")


def render_geo(pairs: list, outdir: str):
    """3. Geography pie."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    vals = [v for _, v in pairs]
    labs = [f"{n}  {v:.0f}%" for n, v in pairs]
    ax.pie(vals, labels=labs, colors=[TEAL, NAVY, GOLD] + PALETTE[3:], startangle=90,
           counterclock=False, textprops={"fontsize": 10})
    ax.set_title("By geography")
    return _save(fig, outdir, "geo")


def render_cap(pairs: list, outdir: str):
    """4. Market-cap bar."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    names = [n for n, _ in pairs]
    vals = [v for _, v in pairs]
    bars = ax.bar(names, vals, color=TEAL, width=0.55)
    ax.bar_label(bars, fmt="%d%%", padding=2, fontsize=10, color=NAVY, fontweight="bold")
    ax.set_ylabel("Weight %"); ax.set_ylim(0, max(vals) * 1.2)
    ax.set_title("By company size"); _declutter(ax); ax.tick_params(axis="x", rotation=0)
    return _save(fig, outdir, "cap")


def render_radar(rows: list, outdir: str):
    """5. Allocation-shape radar. rows = [(dim, you_pct, reference_pct), ...]. Only chart
    with a legend (bottom), per the approved design."""
    if len(rows) < 3:
        return None
    dims = [d for d, _, _ in rows]
    you = [a for _, a, _ in rows]
    ref = [b for _, _, b in rows]
    ang = np.linspace(0, 2 * np.pi, len(dims), endpoint=False).tolist(); ang += ang[:1]
    you += you[:1]; ref += ref[:1]
    fig, ax = plt.subplots(figsize=(5.6, 4.4), subplot_kw=dict(polar=True))
    ax.plot(ang, you, color=TEAL, lw=2, label="You"); ax.fill(ang, you, color=TEAL, alpha=.30)
    ax.plot(ang, ref, color=GOLD, lw=2, ls="--", label="Balanced reference")
    ax.fill(ang, ref, color=GOLD, alpha=.12)
    ax.set_xticks(ang[:-1]); ax.set_xticklabels(dims, fontsize=10)
    ax.set_yticks([20, 40, 60]); ax.set_yticklabels(["20%", "40%", "60%"], fontsize=8,
                                                    color=SLATE)
    ax.set_title("Allocation shape: You vs Balanced", pad=18)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.22), ncol=2, frameon=False,
              fontsize=10)
    return _save(fig, outdir, "radar")


def render_pareto(pairs: list, outdir: str):
    """6. Top-holdings Pareto: bars + cumulative line + 50% marker."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(8.2, 4.0))
    names = [n for n, _ in pairs]
    vals = [v for _, v in pairs]
    cum = np.cumsum(vals)
    bars = ax.bar(names, vals, color=TEAL, width=0.6, label="Holding weight")
    ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=9, color=NAVY)
    ax.plot(names, cum, color=GOLD, lw=2.5, marker="o", ms=5, label="Cumulative")
    ax.axhline(50, color=RED, lw=1, ls=":")
    ax.text(len(names) - 0.4, 51.5, "50% of portfolio", color=RED, fontsize=9, ha="right")
    ax.set_ylabel("% of portfolio"); ax.set_ylim(0, max(62, cum[-1] * 1.15))
    ax.set_title(f"Top-{len(names)} holdings and how fast they add up (Pareto)")
    ax.legend(frameon=False, loc="upper left", fontsize=10)
    _declutter(ax); plt.setp(ax.get_xticklabels(), fontsize=9)
    return _save(fig, outdir, "pareto")


def render_sector(pairs: list, outdir: str):
    """7. Sector barh with the 30% comfort line; gold bar = above the line."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 3.8))
    names = [n for n, _ in pairs][::-1]
    vals = [v for _, v in pairs][::-1]
    bars = ax.barh(names, vals, color=[TEAL if v < 30 else GOLD for v in vals], height=0.6)
    ax.bar_label(bars, fmt="%d%%", padding=3, fontsize=10, color=NAVY, fontweight="bold")
    ax.set_xlabel("Weight %"); ax.set_xlim(0, max(vals) * 1.18)
    ax.axvline(30, color=RED, lw=1, ls=":")
    ax.text(30.5, len(names) - 0.6, "30% comfort line", color=RED, fontsize=8.5)
    ax.set_title("Sector split (gold bar = above comfort line)"); _declutter(ax)
    return _save(fig, outdir, "sector")


def render_overlap(pairs: list, outdir: str):
    """8. Fund-pair overlap barh with 20/40% thresholds; traffic-light colours."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 2.9))
    names = [n for n, _ in pairs][::-1]
    vals = [v for _, v in pairs][::-1]
    cols = [GREEN if v < 20 else (GOLD if v < 40 else RED) for v in vals]
    bars = ax.barh(names, vals, color=cols, height=0.5)
    ax.bar_label(bars, fmt="%d%%", padding=3, fontsize=10, fontweight="bold")
    ax.set_xlabel("Estimated overlap %"); ax.set_xlim(0, max(68, max(vals) * 1.2))
    ax.axvline(20, color=SLATE, lw=1, ls=":"); ax.axvline(40, color=RED, lw=1, ls=":")
    ax.text(20.5, -0.45, "healthy <20%", fontsize=8.5, color=SLATE)
    ax.text(40.5, -0.45, "redundant >40%", fontsize=8.5, color=RED)
    ax.set_title("Fund-pair overlap (colour = health)"); _declutter(ax)
    return _save(fig, outdir, "overlap")


def render_beta(pairs: list, outdir: str):
    """9. Beta ladder with the market=1.0 dashed line; red = 2×+ the market."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 3.8))
    names = [n for n, _ in pairs][::-1]
    vals = [v for _, v in pairs][::-1]
    cols = [TEAL if v <= 1 else GOLD if v < 2 else RED for v in vals]
    bars = ax.barh(names, vals, color=cols, height=0.55)
    ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=10, fontweight="bold")
    ax.axvline(1.0, color=NAVY, lw=1.2, ls="--")
    ax.text(1.02, len(names) - 0.5, "market = 1.0", fontsize=8.5, color=NAVY)
    ax.set_xlabel("Beta vs NIFTY 50"); ax.set_xlim(0, max(3.0, max(vals) * 1.15))
    ax.set_title("Beta ladder (red = swings 2×+ the market)"); _declutter(ax)
    return _save(fig, outdir, "beta")


def render_cost(pairs: list, outdir: str):
    """10. Expense-ratio bar with the 0.3% index-fund zone; red = expensive (>0.9%)."""
    if not pairs:
        return None
    fig, ax = plt.subplots(figsize=(7.0, 3.6))
    names = [n for n, _ in pairs]
    vals = [v for _, v in pairs]
    cols = [RED if v > 0.9 else TEAL for v in vals]
    bars = ax.bar(names, vals, color=cols, width=0.55)
    ax.bar_label(bars, fmt="%.2f%%", padding=2, fontsize=9.5, fontweight="bold")
    ax.axhline(0.3, color=GREEN, lw=1, ls=":")
    ax.text(len(names) - .5, 0.32, "index-fund zone", color=GREEN, fontsize=8.5, ha="right")
    ax.set_ylabel("Expense ratio %"); ax.set_ylim(0, max(1.45, max(vals) * 1.25))
    ax.set_title("What each fund charges (red = expensive)")
    _declutter(ax); plt.setp(ax.get_xticklabels(), rotation=18, ha="right", fontsize=9)
    return _save(fig, outdir, "cost")


def render_feedrag(weighted_cost_pct: float, principal: float, outdir: str):
    """11. Cumulative fee-drag line over 10 years on `principal` at the weighted cost."""
    if not weighted_cost_pct or weighted_cost_pct <= 0 or principal <= 0:
        return None
    rate = weighted_cost_pct / 100.0
    yrs = list(range(1, 11))
    fees = [principal * ((1 + rate) ** i - 1) / 1000 for i in yrs]
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    ax.plot(yrs, fees, color=GOLD, lw=2.5, marker="o", ms=4)
    ax.fill_between(yrs, fees, color=GOLD, alpha=.15)
    ax.annotate(f"₹{fees[-1]:.0f}k by Yr 10", xy=(10, fees[-1]),
                xytext=(6.4, fees[-1] * 0.75), fontsize=10, color=NAVY, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=SLATE))
    ax.set_xlabel("Year"); ax.set_ylabel("Cumulative fees (₹ '000)")
    lakh = principal / 100000
    ax.set_title(f"Fee drag on ₹{lakh:.1f}L at your {weighted_cost_pct:.2f}% weighted cost")
    _declutter(ax)
    return _save(fig, outdir, "feedrag")
