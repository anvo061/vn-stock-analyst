"""Daily news crawler: RSS -> SQLite, with precise ticker tagging.

Everything scraped (titles, summaries, article text) is untrusted data: it is stored and
returned as plain strings and must never be executed or treated as instructions.
"""
from __future__ import annotations

import contextlib
import hashlib
import html
import io
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import xml.etree.ElementTree as ET

import feedparser
from bs4 import BeautifulSoup

from . import core

FEEDS = {
    "cafef_ck": "https://cafef.vn/thi-truong-chung-khoan.rss",
    "cafef_dn": "https://cafef.vn/doanh-nghiep.rss",
    "vietstock_cp": "https://vietstock.vn/830/chung-khoan/co-phieu.rss",
    "vietstock_dn": "https://vietstock.vn/737/doanh-nghiep/hoat-dong-kinh-doanh.rss",
    "vnexpress_kd": "https://vnexpress.net/rss/kinh-doanh.rss",
    "tnck": "https://www.tinnhanhchungkhoan.vn/rss/chung-khoan-2.rss",
    "vneconomy_ck": "https://vneconomy.vn/chung-khoan.rss",
    "tuoitre_kd": "https://tuoitre.vn/rss/kinh-doanh.rss",
    "thanhnien_kd": "https://thanhnien.vn/rss/kinh-te.rss",
}
# Sources published as a news sitemap rather than RSS. FireAnt's robots.txt (checked 07/11/2026)
# disallows /api/ for every bot and does not disallow /bai-viet or this sitemap; its terms say
# nothing on automated access. So only the public sitemap is read: title, time and link are kept,
# full text is fetched only on demand, never stored, and a refusal stops this source for a day.
SITEMAPS = {"fireant": "https://fireant.vn/sitemap-tin-tuc.xml"}
SITEMAP_CLIENT = "stock-analyst-personal/0.1"  # an honest name, not a browser disguise
SITEMAP_MIN_INTERVAL = timedelta(minutes=30)
SITEMAP_BLOCK_PAUSE = timedelta(hours=24)
SITEMAP_MAX_BYTES = 5 * 1024 * 1024
_SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "n": "http://www.google.com/schemas/sitemap-news/0.9"}
_BLOCK_STATUSES = ("HTTP 401", "HTTP 403", "HTTP 429")

ALLOWED_DOMAINS = frozenset(urlsplit(u).hostname.removeprefix("www.") for u in [*FEEDS.values(), *SITEMAPS.values()])
SITEMAP_DOMAINS = frozenset(urlsplit(u).hostname.removeprefix("www.") for u in SITEMAPS.values())

MAX_ARTICLE_REDIRECTS = 3

SYMBOLS_URL = "https://trading.vietcap.com.vn/api/price/symbols/getAll"
SYMBOLS_MAX_AGE = timedelta(days=7)
SYMBOLS_MIN_COUNT = 500  # fewer than this means the endpoint response is broken
LISTED_BOARDS = {"HSX", "HNX", "UPCOM"}  # the endpoint also returns DELISTED

MAX_FUTURE = timedelta(hours=3)
TITLE_DEDUPE_WINDOW = timedelta(hours=48)
MIN_ARTICLE_CHARS = 300

_TRACKING_PREFIXES = ("utm_", "vn_")
_TRACKING_PARAMS = {"fbclid", "gclid", "gidzl", "zarsrc", "mc_cid", "mc_eid", "ref", "rss", "cmpid"}

# Bare uppercase tokens that are NOT read as tickers. Most collide with a real listed symbol
# (checked against the 1,516 active symbols and live titles on 2026-11-07), so a true mention
# of one needs explicit context: "(HOSE: VND)", "mã CEO", "cổ phiếu AAA".
STOPLIST = frozenset("""
    USD EUR JPY CNY GBP KRW AUD HKD TWD CHF CAD SGD THB VND NDT
    GDP CPI PCE PMI FDI FII ODA VAT PIT PPP BOT FTA TPP ESG KPI KCN SDG G20
    CEO CFO CTO COO CIO CPA AGM TCT MST BCA MCP
    ETF IPO OTC ROE ROA EPS NIM NPL NAV CAR ABS SMA HNX VNX VNI CSI SSC SBV
    FED ECB BOJ IMF WTO ADB MAS SEC CDC MIC
    AAA HCM SJC VNA VTV HTV VTC CNN CBS VIR
    BCG GIC AIG BHP TCL TTC VNG VRG BSC TPS SGC BMC CMC AMC AMD ARM UPS APT CFM
    ONE TOP BIG HOT VIP DNA NET ART APP API PET POS BTS CNC LCD PCB PRO
    ICT ITS VAF TTS CAN SEA CAT COM PEN CT3 CT6
""".split())

_TOKEN = re.compile(r"(?<![\w&/.\-])([A-Z][A-Z0-9]{2})(?![\w&\-]|[/.]\w)(?!\s+ETF\b)")
_EXPLICIT = re.compile(
    r"(?i:\b(?:hose|hsx|hnx|upcom)\s*:\s*|\b(?:mã(?: chứng khoán| cổ phiếu| ck)?|cổ phiếu)[:\s]+)"
    r"([A-Z][A-Z0-9]{2})(?![\w&\-])"
)
_EXCHANGE_TAG_AFTER = re.compile(r"\s*\(\s*(?i:hose|hsx|hnx|upcom)\s*:\s*([A-Z0-9]{3})\s*\)")
_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff\u0000-\u0008\u000b\u000c\u000e-\u001f]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    crawled_at TEXT NOT NULL,
    tickers TEXT NOT NULL,
    search_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_title ON articles(title_norm);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(core.VN_TZ).isoformat(timespec="seconds")


def _parse_date(raw: Any, now: datetime | None = None) -> str | None:
    """Return ISO 8601 +07:00, or None for anything unreadable or too far in the future.

    Tuoitre's `11/6/2026 11:06:00 PM` is month-first local time: on 2026-11-07 all 50 live
    entries sat 4 to 502 minutes after the VN-time stamp embedded in their own article URLs.
    """
    text = " ".join(str(raw or "").split())  # also collapses tuoitre's U+202F before PM
    # The stdlib RFC 2822 parser reads year "0001" as 2001; that would be a made-up date.
    if not text or re.search(r"\b\d{1,2} [A-Za-z]{3} (?:0\d{3}|\d|\d{3})\b", text):
        return None
    parsed = None
    for parser in (
        lambda s: datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p"),
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: parsedate_to_datetime(re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)),
    ):
        try:
            parsed = parser(text)
            break
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    if parsed is None or parsed.year < 2000:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=core.VN_TZ)
    if parsed > (now or core.now_vn()) + MAX_FUTURE:
        return None
    return _iso(parsed)


def _clean(raw: Any) -> str:
    """Decode entities (some feeds escape twice), strip tags, NFC, collapse whitespace."""
    text = html.unescape(html.unescape(str(raw or "")))
    if "<" in text:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ")
    return " ".join(_INVISIBLE.sub("", unicodedata.normalize("NFC", text)).split())


def _fold(text: str) -> str:
    """Accent- and case-insensitive form for matching."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return stripped.replace("đ", "d").replace("Đ", "D").casefold()


def _normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", _fold(title)).split())


def _normalise_url(raw: Any) -> tuple[str, str] | None:
    """Return (clean url, dedupe key), or None when the link is not plain http(s)."""
    try:
        parts = urlsplit(str(raw or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    netloc = parts.netloc.lower()
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    ))
    clean = urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
    key = f"{netloc.removeprefix('www.')}{parts.path.rstrip('/')}?{query}"
    return clean, key


def _parse_symbols(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise core.SourceError("Danh sách mã không phải list")
    symbols = sorted({
        row["symbol"] for row in payload
        if isinstance(row, dict) and row.get("type") == "STOCK"
        and row.get("board") in LISTED_BOARDS
        and re.fullmatch(r"[A-Z0-9]{3}", str(row.get("symbol")))
    })
    if len(symbols) < SYMBOLS_MIN_COUNT:
        raise core.SourceError(f"Danh sách mã quá ngắn: {len(symbols)}")
    return symbols


def _load_universe(warnings: list[str]) -> set[str]:
    """Listed symbols: fresh cache, then endpoint, then stale cache; may be empty."""
    path = core.DATA_DIR / "symbols.json"
    cached, age = None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached = set(data["symbols"])
        age = core.now_vn() - datetime.fromisoformat(data["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if cached and age is not None and age < SYMBOLS_MAX_AGE:
        return cached
    try:
        symbols = _parse_symbols(core.fetch_json(
            SYMBOLS_URL, headers=core.VIETCAP_HEADERS, timeout=20, retries=1
        ))
    except core.SourceError as exc:
        fallback = "cache cũ" if cached else "chỉ dùng watchlist"
        warnings.append(f"Không tải được danh sách mã ({fallback}): {exc}")
        return cached or set()
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"fetched_at": _iso(core.now_vn()), "symbols": symbols}), encoding="utf-8"
    )
    tmp.replace(path)
    return set(symbols)


def _load_aliases(warnings: list[str]) -> dict[str, list[re.Pattern[str]]]:
    try:
        symbols = core.load_watchlist()["symbols"]
    except (OSError, ValueError, KeyError) as exc:
        warnings.append(f"Không đọc được watchlist: {exc}")
        return {}
    return {
        symbol: [
            re.compile(rf"(?<!\w){re.escape(unicodedata.normalize('NFC', name))}(?!\w)", re.I)
            for name in [info.get("name"), *info.get("aliases", [])] if name
        ]
        for symbol, info in symbols.items()
    }


def _names_other_company(text: str, end: int, symbol: str) -> bool:
    """True for `... Hòa Phát (HoSE: HPA)`: the exchange tag says which company this really is."""
    tag = _EXCHANGE_TAG_AFTER.match(text, end)
    return bool(tag and tag[1] != symbol)


def _is_shouting(text: str) -> bool:
    """All-caps text makes ordinary words look like tickers, so bare codes mean nothing there."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 20 and sum(c.isupper() for c in letters) > 0.7 * len(letters)


def _tag_tickers(
    texts: list[str], universe: set[str], aliases: dict[str, list[re.Pattern[str]]]
) -> list[str]:
    found = set()
    for text in texts:
        for symbol, patterns in aliases.items():
            for pattern in patterns:
                if any(not _names_other_company(text, m.end(), symbol) for m in pattern.finditer(text)):
                    found.add(symbol)
                    break
        for match in _EXPLICIT.finditer(text):
            if match[1] in universe:
                found.add(match[1])
        if not _is_shouting(text):
            for match in _TOKEN.finditer(text):
                code = match[1]
                if (code in universe and code not in STOPLIST
                        and not _names_other_company(text, match.end(), code)):
                    found.add(code)
    return sorted(found)


@contextlib.contextmanager
def _connect(db_path: str | Path | None):
    path = Path(db_path) if db_path else core.DATA_DIR / "news.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        yield conn


def _parse_feed(content: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Return (raw entries, parser complaint). A BytesIO keeps feedparser off any URL or path."""
    feed = feedparser.parse(io.BytesIO(content))
    if not feed.entries:
        raise core.SourceError(f"Feed không có tin nào (bozo={bool(feed.bozo)})")
    return feed.entries, str(feed.get("bozo_exception")) if feed.bozo else None


def _parse_sitemap(content: bytes) -> list[dict[str, Any]]:
    """Google News sitemap -> entries shaped like feed entries. The summary stays empty on purpose."""
    if len(content) > SITEMAP_MAX_BYTES:
        raise core.SourceError(f"Sitemap quá lớn: {len(content)} byte")
    if b"<!DOCTYPE" in content[:2048].upper() or b"<!ENTITY" in content.upper():
        raise core.SourceError("Sitemap có khai báo DOCTYPE/ENTITY, không đọc")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise core.SourceError(f"Sitemap không phải XML hợp lệ: {exc}") from exc
    entries = []
    for node in root.findall("s:url", _SITEMAP_NS):
        news = node.find("n:news", _SITEMAP_NS)
        entries.append({
            "link": node.findtext("s:loc", namespaces=_SITEMAP_NS),
            "title": news.findtext("n:title", namespaces=_SITEMAP_NS) if news is not None else None,
            "published": news.findtext("n:publication_date", namespaces=_SITEMAP_NS) if news is not None else None,
            "summary": "",
        })
    if not entries:
        raise core.SourceError("Sitemap không có tin nào")
    return entries


def _load_state() -> dict[str, Any]:
    try:
        return json.loads((core.DATA_DIR / "news_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = core.DATA_DIR / "news_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _sitemap_entries(name: str, url: str, now: datetime, state: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch one sitemap unless it was read under 30 minutes ago or recently refused us."""
    entry_state = state.setdefault(name, {})
    blocked_until = entry_state.get("blocked_until")
    if blocked_until and now < datetime.fromisoformat(blocked_until):
        row.update(status="paused", error=f"Bị từ chối truy cập, tạm ngừng đến {blocked_until}")
        return []
    last = entry_state.get("last_fetch")
    if last and now - datetime.fromisoformat(last) < SITEMAP_MIN_INTERVAL:
        row.update(status="skipped", error=f"Đã đọc lúc {last}; mỗi 30 phút tối đa một lần")
        return []
    try:
        response = core.fetch(url, timeout=20, retries=1, headers={"User-Agent": SITEMAP_CLIENT, "Accept": "application/xml"})
    except core.SourceError as exc:
        if any(status in str(exc) for status in _BLOCK_STATUSES):
            entry_state["blocked_until"] = _iso(now + SITEMAP_BLOCK_PAUSE)
        raise
    entry_state["last_fetch"] = _iso(now)
    entry_state.pop("blocked_until", None)
    return _parse_sitemap(response.content)


def _store_entry(conn, entry, source, crawled_at, now, universe, aliases) -> tuple[bool, str | None]:
    """Insert one entry. Return (inserted, problem); problem is 'link', 'title' or 'date'."""
    link = _normalise_url(entry.get("link"))
    if link is None:
        return False, "link"
    url, key = link
    title = _clean(entry.get("title"))
    title_norm = _normalise_title(title)
    if not title_norm:
        return False, "title"
    summary = _clean(entry.get("summary") or entry.get("description"))
    published_at = _parse_date(entry.get("published") or entry.get("updated"), now)
    problem = None if published_at else "date"
    moment = datetime.fromisoformat(published_at or crawled_at)
    duplicate = conn.execute(
        "SELECT 1 FROM articles WHERE title_norm = ? AND COALESCE(published_at, crawled_at) "
        "BETWEEN ? AND ? LIMIT 1",
        (title_norm, _iso(moment - TITLE_DEDUPE_WINDOW), _iso(moment + TITLE_DEDUPE_WINDOW)),
    ).fetchone()
    if duplicate:
        return False, None
    tickers = _tag_tickers([title, summary], universe, aliases)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO articles (id, url, title, title_norm, summary, source, "
        "published_at, crawled_at, tickers, search_text) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (hashlib.sha1(key.encode()).hexdigest()[:16], url, title, title_norm, summary, source,
         published_at, crawled_at, json.dumps(tickers), _fold(f"{title} {summary}")),
    )
    return cursor.rowcount > 0, problem


def crawl(db_path: str | Path | None = None) -> dict[str, Any]:
    """Fetch every feed into SQLite. Idempotent; one failing feed never stops the others."""
    now = core.now_vn()
    fetched_at = _iso(now)
    warnings: list[str] = []
    aliases = _load_aliases(warnings)
    universe = _load_universe(warnings) | set(aliases)
    sources = []
    state = _load_state()
    with _connect(db_path) as conn:
        for name, url in [*FEEDS.items(), *SITEMAPS.items()]:
            row = {"name": name, "status": "ok", "entries": 0, "new": 0, "error": None}
            sources.append(row)
            try:
                if name in SITEMAPS:
                    entries, complaint = _sitemap_entries(name, url, now, state, row), None
                    if row["status"] != "ok":
                        continue  # skipped or paused by design, not a fault
                else:
                    entries, complaint = _parse_feed(core.fetch(url, timeout=20, retries=1).content)
            except Exception as exc:  # any feed fault stays inside this feed
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                warnings.append(f"{name}: {row['error']}")
                continue
            if complaint:
                warnings.append(f"{name}: feed không chuẩn nhưng vẫn đọc được: {complaint}")
            row["entries"] = len(entries)
            problems = {"link": 0, "title": 0, "date": 0}
            for entry in entries:
                inserted, problem = _store_entry(
                    conn, entry, name, fetched_at, now, universe, aliases
                )
                row["new"] += inserted
                if problem and (inserted or problem != "date"):
                    problems[problem] += 1
            conn.commit()
            labels = {"link": "link không hợp lệ", "title": "thiếu tiêu đề", "date": "ngày không đọc được"}
            warnings.extend(
                f"{name}: {count} tin {labels[kind]}" for kind, count in problems.items() if count
            )
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    _save_state(state)
    return {
        "fetched_at": fetched_at,
        "new": sum(row["new"] for row in sources),
        "total": total,
        "sources": sources,
        "warnings": warnings,
    }


def query(
    symbol: str | None = None,
    hours: float | None = 24,
    keyword: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Newest first. Articles without a usable date are placed and windowed by crawled_at."""
    if limit < 1:
        raise ValueError("limit phải >= 1")
    clauses, params = [], []
    if symbol is not None:
        clauses.append("tickers LIKE ?")
        params.append(f'%"{core.validate_symbol(symbol)}"%')
    if hours is not None:
        if hours < 0:
            raise ValueError("hours phải >= 0")
        clauses.append("COALESCE(published_at, crawled_at) >= ?")
        params.append(_iso(core.now_vn() - timedelta(hours=hours)))
    folded = _fold(keyword or "").strip()
    if folded:
        escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("search_text LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, summary, url, source, published_at, crawled_at, tickers "
            f"FROM articles {where} ORDER BY COALESCE(published_at, crawled_at) DESC, id LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    return [{**dict(row), "tickers": json.loads(row["tickers"])} for row in rows]


def retag(db_path: str | Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Re-run ticker tagging on every stored article with the current symbol list, aliases and STOPLIST.

    Tags are written when an article is stored, so a STOPLIST change otherwise only reaches new articles.
    """
    warnings: list[str] = []
    aliases = _load_aliases(warnings)
    universe = _load_universe(warnings) | set(aliases)
    if not universe:
        raise core.SourceError("Không có danh sách mã để gắn lại; giữ nguyên dữ liệu cũ")
    changes = []
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT id, title, summary, tickers FROM articles").fetchall()
        for row in rows:
            old = json.loads(row["tickers"])
            new = _tag_tickers([row["title"], row["summary"] or ""], universe, aliases)
            if new != old:
                changes.append({"id": row["id"], "title": row["title"], "before": old, "after": new})
        if not dry_run:
            conn.executemany("UPDATE articles SET tickers = ? WHERE id = ?", [(json.dumps(c["after"]), c["id"]) for c in changes])
            conn.commit()
    return {"checked": len(rows), "changed": len(changes), "dry_run": dry_run, "examples": changes[:20], "warnings": warnings}


def latest_crawl(db_path: str | Path | None = None) -> str | None:
    """When news was last stored, so a reader can tell how fresh the list is."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(crawled_at) FROM articles").fetchone()
    return row[0] if row else None


def _require_allowed_url(url: Any) -> None:
    """Raise ValueError unless url is plain http(s) on a feed domain or its subdomain."""
    text = str(url)
    if any(c.isspace() or c == "\\" or ord(c) < 32 for c in text):
        raise ValueError(f"URL không hợp lệ: {text!r}")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() not in ("http", "https")
        or "@" in parts.netloc
        or parts.port not in (None, 80, 443)
        or not any(host == d or host.endswith(f".{d}") for d in ALLOWED_DOMAINS)
    ):
        raise ValueError(f"URL không thuộc miền được phép: {text!r}")


def _extract_main_text(soup: BeautifulSoup) -> str:
    """Pick the element holding the most paragraph text, else fall back to article/main/body."""
    for junk in soup(["script", "style", "noscript", "iframe", "nav", "aside", "header",
                      "footer", "form", "figure", "svg", "button"]):
        junk.decompose()
    for hidden in soup.select('[hidden], [style*="display:none"], [style*="display: none"]'):
        hidden.decompose()
    scores: dict[int, tuple[int, Any]] = {}
    for paragraph in soup.find_all("p"):
        parent = paragraph.parent
        size = len(paragraph.get_text(strip=True))
        scores[id(parent)] = (scores.get(id(parent), (0, parent))[0] + size, parent)
    if scores:
        best = max(scores.values(), key=lambda pair: pair[0])[1]
        blocks = [_clean(p.get_text(" ")) for p in best.find_all("p")]
        text = "\n".join(block for block in blocks if block)
        if text:
            return text
    node = soup.find("article") or soup.find("main") or soup.body
    return _clean(node.get_text(" ")) if node else ""


def fetch_article(url: str) -> dict[str, Any]:
    """Fetch one article from a feed domain. The returned text is untrusted data."""
    current = url
    for _ in range(MAX_ARTICLE_REDIRECTS + 1):
        _require_allowed_url(current)  # checked before each hop, so no off-site request is made
        host = (urlsplit(current).hostname or "").lower().removeprefix("www.")
        headers = {"User-Agent": SITEMAP_CLIENT} if host in SITEMAP_DOMAINS else None
        response = core.fetch(current, timeout=20, retries=1, allow_redirects=False, headers=headers)
        if not response.is_redirect:
            break
        current = urljoin(current, response.headers["Location"])
    else:
        raise core.SourceError(f"{url} chuyển hướng quá {MAX_ARTICLE_REDIRECTS} lần")
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise core.SourceError(f"{url} không trả HTML: {content_type!r}")
    soup = BeautifulSoup(response.content, "lxml")
    heading = soup.find("meta", property="og:title")
    title = heading.get("content") if heading else None
    if not title:
        node = soup.find("h1") or soup.find("title")
        title = node.get_text(" ") if node else ""
    text = _extract_main_text(soup)
    warnings = []
    if len(text) < MIN_ARTICLE_CHARS:
        warnings.append(f"Nội dung trích được quá ngắn ({len(text)} ký tự), có thể trích sai")
    return {"url": url, "title": _clean(title), "text": text, "warnings": warnings}
