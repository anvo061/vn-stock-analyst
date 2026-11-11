import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from stock_analyst import collector as C, core, news
from stock_analyst.api import InviteStore, create_app
from stock_analyst.research import FUNDAMENTALS_TTL_S, FundamentalsCache, NewsReader

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=core.VN_TZ)


def rss_item(title, link, date="Wed, 11 Nov 2026 09:00:00 +0700", summary="Đoạn tóm tắt có nội dung bài"):
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><description>{summary}</description></item>"


@pytest.fixture
def news_db(monkeypatch, tmp_path):
    """A real news.db filled through news.crawl, so the reader is tested against the real schema."""
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "now_vn", lambda: NOW)
    monkeypatch.setattr(core, "load_watchlist", lambda: {"symbols": {"HPG": {"name": "Hòa Phát", "aliases": []}}})
    monkeypatch.setattr(news, "_load_universe", lambda warnings: {"HPG", "FPT"})
    monkeypatch.setattr(news, "SITEMAPS", {"fireant": "https://fireant.vn/sitemap-tin-tuc.xml"})
    feed = ("<?xml version='1.0'?><rss><channel>" + rss_item("HPG lãi kỷ lục quý 3", "https://cafef.vn/hpg-1.chn")
            + rss_item("Thị trường chung đi ngang", "https://cafef.vn/tt-2.chn") + "</channel></rss>").encode()
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
               'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://fireant.vn/bai-viet/hpg/1</loc>'
               '<news:news><news:publication_date>2026-11-11T09:30:00+07:00</news:publication_date>'
               '<news:title>HPG: Hòa Phát mở rộng lò cao</news:title></news:news></url></urlset>').encode()

    class Response:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(core, "fetch", lambda url, **k: Response(sitemap if "fireant" in url else feed if url == news.FEEDS["cafef_ck"] else b"<rss/>"))
    news.crawl(tmp_path / "news.db")
    return tmp_path / "news.db"


def test_news_reader_returns_title_source_time_link_only(news_db):
    result = NewsReader(news_db).get("HPG")
    assert [i["source"] for i in result["items"]] == ["fireant", "cafef_ck"]
    assert all(set(i) == {"title", "source", "published_at", "url", "tickers"} for i in result["items"])
    assert "Đoạn tóm tắt" not in json.dumps(result, ensure_ascii=False)  # no teaser or article text is served
    assert result["last_crawl"] == "2026-11-11T10:00:00+07:00" and result["hours"] == 72
    assert len(NewsReader(news_db).get(None)["items"]) == 3


def test_news_reader_drops_non_web_links_and_checks_ranges():
    reader = NewsReader(query=lambda **k: [{"title": "x", "source": "s", "published_at": None, "crawled_at": "t", "url": "javascript:alert(1)", "tickers": []},
                                           {"title": "y", "source": "s", "published_at": "p", "url": "https://cafef.vn/a", "tickers": ["HPG"]}],
                        latest=lambda db_path=None: None)
    assert [i["title"] for i in reader.get("HPG")["items"]] == ["y"]
    for hours, limit in ((0, 10), (24 * 31, 10), (24, 0), (24, 101)):
        with pytest.raises(ValueError):
            reader.get(None, hours=hours, limit=limit)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_fundamentals_cache_reuses_then_refreshes_and_keeps_stale_on_error():
    clock, calls = Clock(), []

    def analyze(symbol):
        calls.append(symbol)
        if len(calls) == 3:
            raise core.SourceError("down")
        return {"symbol": symbol, "warnings": [], "version": len(calls)}

    cache = FundamentalsCache(analyze, clock)
    assert cache.get("FPT")["version"] == 1
    assert cache.get("FPT")["version"] == 1 and calls == ["FPT"]
    clock.now += FUNDAMENTALS_TTL_S
    assert cache.get("FPT")["version"] == 2
    clock.now += FUNDAMENTALS_TTL_S
    stale = cache.get("FPT")
    assert stale["version"] == 2 and "đã lưu" in stale["warnings"][-1]
    with pytest.raises(core.SourceError):
        FundamentalsCache(lambda s: (_ for _ in ()).throw(core.SourceError("down")), clock).get("VCB")


# --- endpoints --------------------------------------------------------------------------

class Quiet:
    name = "q"

    def fetch(self, symbols):
        return []


@pytest.fixture
def client(news_db, tmp_path):
    collector = C.Collector([Quiet()], ["HPG", "FPT"])
    store = InviteStore(tmp_path / "invites.json")
    token = store.add("An")
    analysed = []

    def analyze(symbol):
        analysed.append(symbol)
        return {"symbol": symbol, "red_flags": [{"rule": "LOW_CASH_CONVERSION", "severity": "medium", "description": "x", "values": {}}],
                "integrity": {"passed": False}, "warnings": ["w"]}

    app = create_app(collector, store, [], news_reader=NewsReader(news_db), fundamentals=FundamentalsCache(analyze))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        test_client.analysed = analysed
        yield test_client


def test_news_endpoint(client):
    body = client.get("/api/news", params={"symbol": "HPG"}).json()
    assert len(body["items"]) == 2 and body["symbol"] == "HPG"
    assert client.get("/api/news").status_code == 200
    assert client.get("/api/news", params={"symbol": "VCB"}).status_code == 404  # not on this board
    assert client.get("/api/news", params={"symbol": "../x"}).status_code == 400
    assert client.get("/api/news", params={"hours": 0}).status_code == 400
    assert client.get("/api/news", headers={"Authorization": ""}).status_code == 401


def test_fundamentals_endpoint_returns_flags_integrity_and_warnings(client):
    body = client.get("/api/fundamentals", params={"symbol": "FPT"}).json()
    assert body["red_flags"][0]["rule"] == "LOW_CASH_CONVERSION" and body["integrity"] == {"passed": False} and body["warnings"] == ["w"]
    client.get("/api/fundamentals", params={"symbol": "FPT"})
    assert client.analysed == ["FPT"]  # cached
    assert client.get("/api/fundamentals", params={"symbol": "VCB"}).status_code == 404
    assert client.get("/api/fundamentals").status_code == 422
    assert client.get("/api/fundamentals", params={"symbol": "FPT"}, headers={"Authorization": ""}).status_code == 401


def test_fundamentals_source_failure_is_a_502(news_db, tmp_path):
    collector = C.Collector([Quiet()], ["FPT"])
    store = InviteStore(tmp_path / "i.json")
    token = store.add("An")
    cache = FundamentalsCache(lambda s: (_ for _ in ()).throw(core.SourceError("down")))
    with TestClient(create_app(collector, store, [], fundamentals=cache, news_reader=None)) as test_client:
        headers = {"Authorization": f"Bearer {token}"}
        assert test_client.get("/api/fundamentals", params={"symbol": "FPT"}, headers=headers).status_code == 502
        assert test_client.get("/api/news", headers=headers).status_code == 503
