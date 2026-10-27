"""Order status and trade side, read from the words a broker actually writes.

  * "Not executed" counted as EXECUTED: the status was tokenised into words and any word in the
    executed list won, so the negation was ignored and an order that never happened became a
    trade. The importer exists to exclude exactly these rows.
  * "SIP Redemption" read as a BUY: "sip" is a buy word and was checked first, so the redemption
    (a SELL) was imported as a purchase of the same size - in the wrong direction, so a book that
    had sold out appeared to have doubled up.

All figures invented. Hermetic.
"""
import os
import tempfile

import pytest

from xray_agent.adapters import broker_import as bi
from xray_agent.domain.transactions import BUY, SELL


@pytest.mark.parametrize("status", [
    "Not executed", "NOT EXECUTED", "Not Filled", "Unexecuted", "Not completed", "Unsuccessful",
    "Order not executed", "Non-executed", "Incomplete", "Never executed",
    "Cancelled", "Rejected", "Failed", "Pending",
])
def test_a_negated_or_failed_status_is_not_an_executed_order(status):
    assert bi.order_executed(status) is False, status


@pytest.mark.parametrize("status", ["Executed", "Completed", "Filled", "Success", "Traded",
                                    "Order executed", "Fully executed", "Complete"])
def test_a_plain_executed_status_still_counts(status):
    assert bi.order_executed(status) is True, status


def test_an_unrecognised_status_is_still_unknown_not_silently_dropped():
    """The existing rule: an unrecognised status is KEPT and surfaced, never dropped."""
    assert bi.order_executed("Awaiting settlement review") is None


@pytest.mark.parametrize("label", [
    "SIP Redemption", "Redemption", "SIP redeemed", "Systematic Withdrawal", "SWP",
    "Sell", "Switch out", "Partial Redemption", "Purchase Redemption",
])
def test_a_redemption_is_a_sell_even_when_it_mentions_a_sip(label):
    assert bi.parse_side(label, 10.0) == SELL, label


@pytest.mark.parametrize("label", ["Buy", "SIP", "Purchase", "Systematic Investment",
                                   "SIP Purchase", "Additional Purchase", "Subscription"])
def test_purchases_are_still_buys(label):
    assert bi.parse_side(label, 10.0) == BUY, label


def test_a_not_executed_row_is_excluded_and_counted_end_to_end():
    csv = ("Date,Symbol,Stock name,Type,Quantity,Price,Order status\n"
           "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n"
           "2026-01-06,INFY,Infosys,BUY,99,1500,Not executed\n")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(csv)
    try:
        holdings, notes = bi.read_broker_csv(f.name, resolve_funds=False)
    finally:
        os.unlink(f.name)
    assert len(holdings) == 1
    assert sum(t.units for t in holdings[0]["transactions"]) == 10
    assert notes["excluded_not_executed"] == 1
