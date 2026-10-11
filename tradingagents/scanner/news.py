"""Headline-only market news, via public RSS feeds -- no API key needed.

Deliberately just headline + source + time + link, not full article text:
this answers "what's moving the market right now," which is what the
scanner's other tabs need context for, not a news reader. One feed being
down never blanks the tab -- each is fetched independently and failures are
silently skipped, since partial news beats none.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingScannerBot/1.0)"}
_TIMEOUT = 10

_CRYPTO_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]
_STOCK_FEEDS = [
    ("CNBC Markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
]

_EPOCH_UTC = datetime.min.replace(tzinfo=timezone.utc)

# Keyword-based impact heuristic, forex-factory-style (high/medium/low) --
# there's no real "scheduled event + historical volatility" calendar behind
# an RSS headline the way there is for macro releases, so this is a rough
# "would a trader's head turn" classifier based on the words in the
# headline itself, not a calibrated impact score. Covers both markets in one
# list since a dashboard showing both benefits from one consistent bar.
_HIGH_IMPACT_KEYWORDS = (
    "fed", "fomc", "rate hike", "rate cut", "interest rate", "cpi", "inflation",
    "sec charges", "sec sues", "etf approval", "etf approved", "hack", "exploit",
    "crash", "plunge", "collapse", "bankrupt", "default", "ban", "lawsuit",
    "fraud", "liquidat", "halving", "all-time high", "record high", "record low",
    "emergency", "recession", "shutdown", "war", "sanctions", "flash crash",
)
_MEDIUM_IMPACT_KEYWORDS = (
    "upgrade", "downgrade", "earnings", "ipo", "merger", "acquisition",
    "partnership", "listing", "delisting", "outlook", "forecast", "powell",
    "yellen", "regulation", "lawmaker", "congress", "stimulus", "tariff",
    "layoff", "guidance", "jobs report", "unemployment",
)
_IMPACT_RANK = {"high": 2, "medium": 1, "low": 0}


def _classify_impact(title: str) -> str:
    lowered = title.lower()
    if any(kw in lowered for kw in _HIGH_IMPACT_KEYWORDS):
        return "high"
    if any(kw in lowered for kw in _MEDIUM_IMPACT_KEYWORDS):
        return "medium"
    return "low"


@dataclass
class NewsItem:
    title: str
    source: str
    published: datetime | None
    url: str
    impact: str = "low"  # "high" | "medium" | "low" -- see _classify_impact


def _parse_feed(source: str, url: str) -> list[NewsItem]:
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        published = None
        pub_raw = item.findtext("pubDate")
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append(
            NewsItem(title=title, source=source, published=published, url=link, impact=_classify_impact(title))
        )
    return items


def _fetch_feeds(feeds: list[tuple[str, str]], limit: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    for source, url in feeds:
        try:
            for item in _parse_feed(source, url):
                # The same story genuinely does get syndicated under the
                # identical URL across two feeds from the same publisher
                # (seen between CNBC's Markets and Finance feeds) -- keep
                # the first copy only, not a duplicate row.
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                items.append(item)
        except Exception:
            continue  # one dead/slow feed shouldn't blank the whole tab
    # High impact floats to the top (forex-factory-style grouping), newest
    # first within the same impact tier.
    items.sort(key=lambda n: (_IMPACT_RANK[n.impact], n.published or _EPOCH_UTC), reverse=True)
    return items[:limit]


def fetch_crypto_news(limit: int = 40) -> list[NewsItem]:
    """Latest crypto headlines across CoinDesk + Cointelegraph, newest first."""
    return _fetch_feeds(_CRYPTO_FEEDS, limit)


def fetch_stock_news(limit: int = 40) -> list[NewsItem]:
    """Latest general stock-market headlines across CNBC's Markets + Finance feeds."""
    return _fetch_feeds(_STOCK_FEEDS, limit)
