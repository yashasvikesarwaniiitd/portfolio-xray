"""The advice guardrail and the number check, against phrasings that got through.

The guardrail only knew buy / sell / exit / trim style verbs, so the most natural ways to tell
someone what to do with a holding passed: "You should hold HDFC Bank", "Consider whether to
hold or reduce...", "You might want to cut exposure...". "Hold" is one of the three words the
project promises never to say. And the number check accepted "3x" against an evidence value of
2.6, because rounding to a whole number is allowed and 2.6 rounds to 3.

The second list in each group is as important as the first: analytics prose that merely
contains the same words must keep passing, or every honest finding gets rejected.
"""
import pytest

from xray_agent.domain import policies as p

ADVICE = [
    "You should hold HDFC Bank.",
    "You should hold HDFC Bank for the long term.",
    "Consider whether to hold or reduce your exposure to banks.",
    "You might want to cut exposure to the IT sector.",
    "You may want to reduce your position in Infosys.",
    "Investors should keep holding these funds.",
    "You should stay invested through the fall.",
    "I would hold on to the mid-cap fund.",
    "I recommend reducing your exposure to small caps.",
    "We suggest that you cut your holding in TCS.",
    "Consider trimming the largest position.",
    "Consider cutting exposure to financials.",
    "Hold HDFC Bank.",
    "Reduce your exposure to banks.",
    "You ought to sell the underperformers.",
    "It would be wise to reduce concentration in one stock.",
    "You must exit this fund.",
    "Better to hold than to sell.",
    # the old ones must still be caught
    "You should sell everything immediately.",
    "Time to book profits.",
    "Switch to an index fund.",
]

ANALYTICS = [
    "The portfolio's largest holdings are HDFC Bank and Infosys.",
    "You hold 12 stocks and 3 funds.",
    "Banks are 31% of the portfolio, and you hold them in three forms.",
    "The estimate should hold under normal market conditions.",
    "The RBI rate cut helped bank stocks this quarter.",
    "A cut in the expense ratio lowered the drag by 0.2%.",
    "You can add another file to see the combined portfolio.",
    "Consider the sector mix when reading the concentration figure.",
    "The holding period was 14 months.",
    "Reducing the fee from 1.2% to 0.9% would lower the drag.",
    "Tracking error fell after the index fund was added.",
    "Past returns are not a guide; hold-period returns vary.",
    "This fund's cash holdings rose to 4%.",
    "The sell-off in March cut the value of the book by 8%.",
    "Which of the 7 positions still earn a slot?",
    "What is your written answer: add, hold, or freeze?",
    "The data suggests a cut in exposure happened in March.",
    "You might want to know why the portfolio fell.",
]


@pytest.mark.parametrize("text", ADVICE)
def test_advice_is_caught(text):
    assert p.violates_guardrail(text), text


@pytest.mark.parametrize("text", ANALYTICS)
def test_analytics_prose_is_not_flagged(text):
    assert not p.violates_guardrail(text), text


# --- the number check -------------------------------------------------------------------------

def test_a_whole_number_is_not_accepted_for_a_figure_it_is_far_from():
    """2.6 'rounds to 3' at zero decimals, but 3 is 15% away from it: a different claim."""
    assert p.unsupported_numbers("Sharpe is about 3x.", {2.6}) == ["3"]


@pytest.mark.parametrize("stated,evidence", [
    ("14.6", 14.6), ("15", 14.6), ("12,450", 12450.5), ("12450", 12450.5),
    ("175,490.87", 175490.87), ("1,75,491", 175490.87), ("0.5", 0.48), ("-4.2", 4.2),
    ("41", 41.4), ("3", 3.0), ("0", 0.0),
])
def test_honest_rounding_still_passes(stated, evidence):
    assert p.unsupported_numbers(f"It was {stated}.", {evidence}) == []


@pytest.mark.parametrize("stated,evidence", [("3", 2.6), ("5", 4.5), ("2", 1.5), ("62", 61.4)])
def test_rounding_that_changes_the_figure_is_refused(stated, evidence):
    assert p.unsupported_numbers(f"It was {stated}.", {evidence}) == [stated]
