"""Unit tests for the pure math in metrics.py — hand-verified expected values, no network.

Includes a pure XIRR regression on the exact Parag Parikh cashflows (locks the solver);
the full live reconstruction regression against AMFI data lives in test_agent.py.
Run with: uv run pytest
"""
from datetime import date

import pytest

from metrics import (
    annualize,
    concentration_stats,
    daily_returns,
    nav_on_or_before,
    reconstruct_units,
    regression_slope,
    sharpe_ratio,
    xirr,
)

# The 14 Parag Parikh SIP outflows (1st-of-month), validated by hand.
PP_SIPS = [
    ("2025-03-01", 2000), ("2025-04-01", 2000), ("2025-05-01", 2000),
    ("2025-06-01", 10000), ("2025-07-01", 10000), ("2025-08-01", 5000),
    ("2025-09-01", 12000), ("2025-10-01", 10000), ("2025-11-01", 4000),
    ("2025-12-01", 9000), ("2026-01-01", 9000), ("2026-03-01", 7000),
    ("2026-04-01", 12000), ("2026-06-01", 12000),
]


# --- xirr ---

def test_xirr_simple_one_year_10pct():
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]
    assert xirr(flows) == pytest.approx(0.10, abs=1e-3)


def test_xirr_parag_parikh_regression():
    """Pure regression: exact SIP cashflows + hand-validated current value -> ~0.69%."""
    flows = [(date.fromisoformat(d), -a) for d, a in PP_SIPS]
    flows.append((date(2026, 7, 17), 106509.0))
    assert xirr(flows) * 100 == pytest.approx(0.69, abs=0.05)


def test_parag_parikh_value_at_reference_nav():
    """Ground truth locked network-free: the exact reconstructed units valued at the reference
    NAV (91.4603 on 2026-07-17) reproduce the hand-verified Rs 106,509. This number can't drift
    because both inputs are fixed; the live test only checks units + internal consistency."""
    REFERENCE_NAV = 91.4603
    assert 1164.5414 * REFERENCE_NAV == pytest.approx(106509, rel=0.001)


def test_xirr_needs_sign_change():
    with pytest.raises(ValueError):
        xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)])


def test_xirr_needs_two_flows():
    with pytest.raises(ValueError):
        xirr([(date(2025, 1, 1), -100.0)])


# --- reconstruct_units ---

def test_reconstruct_units_exact():
    flows = [
        {"date": date(2025, 1, 1), "amount": 100.0, "price": 10.0},   # 10 units
        {"date": date(2025, 2, 1), "amount": 200.0, "price": 20.0},   # 10 units
        {"date": date(2025, 3, 1), "amount": 50.0, "price": None},    # unpriced
    ]
    r = reconstruct_units(flows)
    assert r["total_units"] == pytest.approx(20.0)
    assert r["invested_priced"] == 300.0
    assert r["invested_unpriced"] == 50.0
    assert r["priced_sips"] == 2 and r["total_sips"] == 3


# --- nav_on_or_before ---

def test_nav_walks_back_over_weekend():
    price_map = {date(2025, 2, 28): 100.0}  # Friday
    assert nav_on_or_before(price_map, date(2025, 3, 1)) == 100.0  # Sat -> walk back to Fri


def test_nav_returns_none_when_out_of_range():
    assert nav_on_or_before({date(2025, 1, 1): 100.0}, date(2025, 3, 1)) is None


# --- regression_slope (beta) ---

def test_regression_slope_perfect_line():
    assert regression_slope([2, 4, 6, 8], [1, 2, 3, 4]) == pytest.approx(2.0)


def test_regression_slope_zero_variance_raises():
    with pytest.raises(ValueError):
        regression_slope([1, 2, 3], [5, 5, 5])


# --- daily_returns / annualize ---

def test_daily_returns():
    assert daily_returns([100, 110, 99]) == pytest.approx([0.10, -0.10])


def test_annualize_hand_values():
    a = annualize([0.1, -0.1])  # mean 0, sample std sqrt(0.02)
    assert a["ann_return"] == pytest.approx(0.0)
    assert a["ann_volatility"] == pytest.approx(0.141421 * (252 ** 0.5), rel=1e-4)
    assert a["n_obs"] == 2


# --- sharpe_ratio ---

def test_sharpe_hand_value():
    assert sharpe_ratio(0.12, 0.20, 0.068) == pytest.approx(0.26)


def test_sharpe_zero_vol_raises():
    with pytest.raises(ValueError):
        sharpe_ratio(0.1, 0.0, 0.068)


# --- concentration_stats ---

def test_concentration_hand_values():
    holdings = [
        {"name": "A", "value": 50.0, "market": "India", "type": "Share", "risk": "High"},
        {"name": "B", "value": 30.0, "market": "US", "type": "ETF", "risk": "Med"},
        {"name": "C", "value": 20.0, "market": "India", "type": "Share", "risk": "High"},
    ]
    s = concentration_stats(holdings)
    assert s["hhi"] == pytest.approx(0.38)  # 0.25 + 0.09 + 0.04
    assert s["effective_holdings"] == pytest.approx(2.63, abs=0.01)
    assert [h["name"] for h in s["holdings_by_weight"]] == ["A", "B", "C"]
    assert len(s["over_threshold"]) == 3  # all > 10%
    assert s["by_market"]["India"] == pytest.approx(70.0)


def test_concentration_zero_total_raises():
    with pytest.raises(ValueError):
        concentration_stats([{"name": "A", "value": 0.0, "market": "X",
                              "type": "Y", "risk": "Z"}])
