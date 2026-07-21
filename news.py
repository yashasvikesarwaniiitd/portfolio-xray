"""Google News RSS fetch (free, no API key) for citable holding news.

Every item carries its source article URL so downstream synthesis can cite it. All failure
modes — no results, network error, rate-limiting, malformed feed — degrade to a clean
"news unavailable" result and never raise, so one flaky fetch can't crash a digest sweep.
"""
import time
from datetime import date, datetime

import feedparser
import requests

# Locale params bias Google News toward Indian coverage for Indian holdings.
_RSS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
_HEADERS = {"User-Agent": "Mozilla/5.0 (PortfolioXray news reader)"}


def news_query(name: str) -> str:
    """Build the search query from a holding's human name (the CSV `Where` column).
    News search works far better on 'Reliance Industries' than on 'RELIANCE.NS'."""
    return str(name).strip()


def _parse_published(entry) -> str | None:
    tp = entry.get("published_parsed") or entry.get("updated_parsed")
    if tp:
        return date(tp.tm_year, tp.tm_mon, tp.tm_mday).isoformat()
    return entry.get("published")


def _source_of(entry) -> str:
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    title = entry.get("title", "")
    # Google News titles are usually "Headline - Source"; fall back to the trailing part.
    return title.rsplit(" - ", 1)[-1] if " - " in title else "unknown"


def fetch_news(query: str, limit: int = 8, timeout: int = 15) -> dict:
    """Return {"query", "articles": [{headline, source, published, url}], "count"} or, on any
    failure, that same shape with "error" set and an empty article list."""
    url = _RSS.format(q=requests.utils.quote(query))
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code == 429:
            return {"query": query, "articles": [], "count": 0,
                    "error": "rate-limited by Google News; try again later"}
        if resp.status_code != 200:
            return {"query": query, "articles": [], "count": 0,
                    "error": f"news feed returned HTTP {resp.status_code}"}
        feed = feedparser.parse(resp.content)
    except requests.RequestException as e:
        return {"query": query, "articles": [], "count": 0,
                "error": f"news fetch failed: {e}"}
    except Exception as e:  # malformed feed / parser hiccup
        return {"query": query, "articles": [], "count": 0,
                "error": f"could not parse news feed: {e}"}

    articles = []
    for entry in feed.entries[:limit]:
        headline = entry.get("title", "").rsplit(" - ", 1)[0].strip()
        link = entry.get("link")
        if not headline or not link:
            continue
        articles.append({
            "headline": headline,
            "source": _source_of(entry),
            "published": _parse_published(entry),
            "url": link,
        })
    return {"query": query, "articles": articles, "count": len(articles)}
