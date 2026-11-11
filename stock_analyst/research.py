"""News and financial-statement data for the web app, shaped for sharing and cached for the tunnel.

News items carry title, source, time and link only. FireAnt's terms say nothing on reuse, so its
text is never served; RSS teasers are left out too, which keeps one rule for every source.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import core, fundamentals, news

NEWS_DEFAULT_HOURS = 72
NEWS_MAX_HOURS = 24 * 30
NEWS_DEFAULT_LIMIT = 30
NEWS_MAX_LIMIT = 100
FUNDAMENTALS_TTL_S = 6 * 3600  # statements change once a quarter; prices inside valuation are a day old anyway
FUNDAMENTALS_MAX_CACHED = 100


class NewsReader:
    def __init__(self, db_path: str | Path | None = None, query: Callable[..., list[dict[str, Any]]] = news.query,
                 latest: Callable[..., str | None] | None = None):
        self.db_path, self.query = db_path, query
        self.latest = latest or (lambda db_path=None: news.latest_crawl(db_path))

    def get(self, symbol: str | None, hours: int = NEWS_DEFAULT_HOURS, limit: int = NEWS_DEFAULT_LIMIT) -> dict[str, Any]:
        if not 1 <= hours <= NEWS_MAX_HOURS or not 1 <= limit <= NEWS_MAX_LIMIT:
            raise ValueError(f"hours phải từ 1 đến {NEWS_MAX_HOURS}, limit từ 1 đến {NEWS_MAX_LIMIT}")
        rows = self.query(symbol=symbol, hours=hours, limit=limit, db_path=self.db_path)
        items = [
            {"title": row["title"], "source": row["source"], "published_at": row.get("published_at") or row.get("crawled_at"),
             "url": row["url"], "tickers": row.get("tickers") or []}
            for row in rows if urlsplit(str(row.get("url"))).scheme in ("http", "https")
        ]
        return {"symbol": symbol, "hours": hours, "items": items, "last_crawl": self.latest(db_path=self.db_path),
                "note": "Chỉ gồm tin đã cào và được gắn mã; thiếu tin không có nghĩa là không có tin."}


class FundamentalsCache:
    """One upstream analysis per symbol at a time, kept for hours, and stale data beats an error."""

    def __init__(self, analyze: Callable[[str], dict[str, Any]] = fundamentals.analyze, clock: Callable[[], float] = time.time):
        self.analyze, self.clock = analyze, clock
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(symbol, threading.Lock())

    def get(self, symbol: str) -> dict[str, Any]:
        with self._lock_for(symbol):
            hit = self.cache.get(symbol)
            if hit and self.clock() - hit[0] < FUNDAMENTALS_TTL_S:
                return hit[1]
            try:
                result = self.analyze(symbol)
            except (core.SourceError, ValueError, KeyError, TypeError, IndexError) as exc:
                if not hit:
                    raise core.SourceError(f"Không lấy được BCTC của {symbol}: {exc}") from exc
                stale = hit[1]
                return {**stale, "warnings": [*stale.get("warnings", []), "Nguồn BCTC đang lỗi, đây là dữ liệu đã lưu trước đó."]}
            with self._guard:
                self.cache.pop(symbol, None)
                self.cache[symbol] = (self.clock(), result)
                if len(self.cache) > FUNDAMENTALS_MAX_CACHED:
                    self.cache.pop(next(iter(self.cache)))
            return result
