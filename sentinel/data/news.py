"""Indian financial news via RSS (no scraping fragility, no API keys)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("sentinel.news")

FEED_TIMEOUT_SEC = 8.0   # per-feed ceiling; the scan thread must never block

FEEDS = {
    "MoneyControl Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "MoneyControl Business": "https://www.moneycontrol.com/rss/business.xml",
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "LiveMint Markets": "https://www.livemint.com/rss/markets",
}


@dataclass
class Headline:
    title: str
    source: str
    link: str
    published: str
    sentiment: str = ""      # filled by the sentiment module
    score: float = 0.0


class NewsFetcher:
    def __init__(self, cache_minutes: int = 10):
        self._cache: list[Headline] = []
        self._fetched_at = 0.0
        self._ttl = cache_minutes * 60

    def headlines(self, limit: int = 30, force: bool = False) -> list[Headline]:
        if not force and self._cache and (time.time() - self._fetched_at) < self._ttl:
            return self._cache[:limit]
        import socket

        import feedparser
        items: list[Headline] = []
        # feedparser.parse() takes no timeout argument and urllib's default
        # socket timeout is None — i.e. infinite. A host that accepts the
        # connection then never answers would block the scan thread forever,
        # and the try/except below cannot help because a hang never raises.
        # This runs on the engine thread, so that stalls the whole product.
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(FEED_TIMEOUT_SEC)
        try:
            for source, url in FEEDS.items():
                try:
                    parsed = feedparser.parse(url)
                    for e in parsed.entries[:12]:
                        items.append(Headline(
                            title=e.get("title", "").strip(),
                            source=source,
                            link=e.get("link", ""),
                            published=e.get("published", ""),
                        ))
                except Exception as exc:
                    log.warning("feed %s failed: %s", source, exc)
        finally:
            socket.setdefaulttimeout(prev_timeout)
        seen: set[str] = set()
        unique = []
        for h in items:
            key = h.title.lower()[:80]
            if key and key not in seen:
                seen.add(key)
                unique.append(h)
        if unique:
            self._cache = unique
            self._fetched_at = time.time()
        return self._cache[:limit]

    def for_symbol(self, keywords: list[str], limit: int = 10) -> list[Headline]:
        kws = [k.lower() for k in keywords]
        return [h for h in self.headlines(limit=100)
                if any(k in h.title.lower() for k in kws)][:limit]
