"""With no portfolio loaded the analyst must say so, not answer about the server's sample.

The failure: a visitor who had uploaded nothing asked "How concentrated am I?" and was told
about an ETF-heavy book with Bitcoin in it - the server's built-in sample - as if it were
theirs. Everything that does not NEED a portfolio must keep working without one.

Hermetic: the router and the tool loop are replaced, so no network and no API key.
"""
import pytest
from fastapi.testclient import TestClient

import agent
import api

CLIENT = object()


@pytest.fixture
def route(monkeypatch):
    """Set the category the router returns, and make any tool-loop run a test failure."""
    def set_category(category):
        monkeypatch.setattr(agent.router, "classify",
                            lambda *a, **k: {"category": category, "degraded": False})
    monkeypatch.setattr(agent, "run_investigator",
                        lambda *a, **k: pytest.fail("the tool loop ran with no portfolio"))
    monkeypatch.setattr(agent.logger, "log_query", lambda *a, **k: None)
    return set_category


def ask(text="How concentrated am I?", **kw):
    return agent.answer_query(CLIENT, [], text, {}, log=False, **kw)


def test_an_analytics_question_with_no_portfolio_says_there_is_none(route):
    route("analytics")
    r = ask(has_portfolio=False)
    assert r["answer"] == agent.NO_PORTFOLIO_REPLY
    assert r["tools_used"] == [] and r["refused"] is False and r["no_portfolio"] is True
    assert "upload" in r["answer"].lower() and "sample" in r["answer"].lower()


def test_the_default_still_runs_the_analysis_so_existing_callers_are_unchanged(monkeypatch):
    class State:
        summary, ledger, outcome = "analysed", {}, "COMPLETE"
    monkeypatch.setattr(agent.router, "classify",
                        lambda *a, **k: {"category": "analytics", "degraded": False})
    monkeypatch.setattr(agent, "run_investigator", lambda *a, **k: State())
    r = ask()                                   # has_portfolio defaults to True
    assert r["answer"] == "analysed" and "no_portfolio" not in r


def test_a_question_that_needs_no_portfolio_is_still_answered(route, monkeypatch):
    """"What is XIRR?" does not depend on anyone's holdings."""
    route("education")
    monkeypatch.setattr(agent, "answer_without_tools", lambda *a, **k: "XIRR is...")
    r = ask("What is XIRR?", has_portfolio=False)
    assert r["answer"] == "XIRR is..." and r["category"] == "education"


def test_advice_is_still_refused_with_no_portfolio(route):
    """The refusal is a hard rule and must not depend on whether anything is loaded."""
    route("advice")
    r = ask("Should I sell my worst fund?", has_portfolio=False)
    assert r["refused"] is True and r["category"] == "advice"
    assert r["answer"] != agent.NO_PORTFOLIO_REPLY


def test_off_topic_keeps_its_own_redirect(route):
    route("offtopic")
    assert ask("What's the weather?", has_portfolio=False)["answer"] == agent.OFFTOPIC_REDIRECT


def test_news_does_not_read_the_servers_sample_when_there_is_no_portfolio(monkeypatch):
    """The news path looks up a holding's name to tidy its search query. With no portfolio it
    must not go and read the sample book to do that."""
    seen = []
    real = agent.read_portfolio
    monkeypatch.setattr(agent, "read_portfolio",
                        lambda path, *a, **k: (seen.append(path), real(path, *a, **k))[1])
    assert agent._news_query_from_text("latest news on Infosys", "") == "latest news on Infosys"
    assert seen == [""], "it asked for no file, so it could not have read the sample"


# --- the API -----------------------------------------------------------------------------------

http = TestClient(api.app)


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake(client, history, message, state, path=None, has_portfolio=True, **kw):
        calls.append({"path": path, "has_portfolio": has_portfolio})
        return {"answer": "ok", "category": "analytics", "refused": False, "tools_used": []}

    monkeypatch.setattr(agent, "answer_query", fake)
    monkeypatch.setattr(api, "client", lambda: object())
    monkeypatch.setattr(api, "_bump", lambda *a, **k: True)
    return calls


def test_the_frontend_flag_reaches_the_agent(captured):
    http.post("/chat", json={"message": "How concentrated am I?", "no_portfolio": True})
    assert captured[-1]["has_portfolio"] is False


def test_an_uploaded_portfolio_always_wins_over_the_flag(captured):
    csv = ("Date,Symbol,Stock name,Type,Quantity,Price,Order status\n"
           "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n")
    http.post("/chat", json={"message": "x", "portfolio_csv": csv, "no_portfolio": True})
    assert captured[-1]["has_portfolio"] is True


def test_a_caller_that_says_nothing_still_gets_the_sample(captured):
    """The documented demo behaviour for a direct API caller: no CSV and no flag means the
    server's sample. Only a frontend that knows nothing is loaded sets the flag."""
    http.post("/chat", json={"message": "x"})
    assert captured[-1]["has_portfolio"] is True
