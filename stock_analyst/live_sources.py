"""Live quote adapters. Every source returns the same quote shape, prices in VND, value in VND."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from . import core, technical

CHUNK_SIZE = 2000  # both sources answered 1,516 symbols in one call in under a second on 07/11/2026
REQUEST_TIMEOUT_S = 8
UNIVERSE_URL = "https://trading.vietcap.com.vn/api/price/symbols/getAll"
UNIVERSE_MAX_AGE_S = 86400
UNIVERSE_MIN_COUNT = 500  # fewer than this means the endpoint response is broken
EXCHANGE_BY_BOARD = {"HSX": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}
SESSION_START_HOUR_MIN = (9, 15)  # the first bar of a HOSE session, right after ATO
CHART_URL = "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart"


class QuoteSource(Protocol):
    name: str

    def fetch(self, symbols: list[str]) -> list[dict[str, Any]]: ...


def parse_universe(payload: Any) -> list[dict[str, Any]]:
    """Active listed stocks only. The endpoint also returns delisted codes, warrants, bonds and futures."""
    entries = {}
    for row in payload if isinstance(payload, list) else []:
        symbol, exchange = str(row.get("symbol")), EXCHANGE_BY_BOARD.get(row.get("board"))
        if row.get("type") == "STOCK" and exchange and len(symbol) == 3 and symbol.isalnum():
            entries[symbol] = {"symbol": symbol, "exchange": exchange, "name": row.get("organShortName") or row.get("organName") or ""}
    return sorted(entries.values(), key=lambda entry: entry["symbol"])


def load_universe() -> list[dict[str, Any]]:
    """Every listed stock: fresh cache, then the endpoint, then a stale cache, then the watchlist alone."""
    path = core.DATA_DIR / "live" / "universe.json"
    cached = None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - path.stat().st_mtime < UNIVERSE_MAX_AGE_S:
            return cached
    except (OSError, ValueError):
        pass
    try:
        fresh = parse_universe(core.fetch_json(UNIVERSE_URL, headers=core.VIETCAP_HEADERS, timeout=20))
        if len(fresh) >= UNIVERSE_MIN_COUNT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
            return fresh
    except (core.SourceError, AttributeError):
        pass
    if cached:
        return cached
    return [{"symbol": symbol, "exchange": None, "name": info.get("name", "")} for symbol, info in core.load_watchlist()["symbols"].items()]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None  # sources put text such as "ATO" in price fields during auctions
    return number if number == number else None


def _levels(prices: list[Any], volumes: list[Any]) -> list[dict[str, float]]:
    levels = []
    for price, volume in zip(prices, volumes):
        price, volume = _number(price), _number(volume)
        if price and volume is not None:
            levels.append({"price": price, "volume": volume})
    return levels


def _quote(symbol: Any, source: str, **fields: Any) -> dict[str, Any] | None:
    """A stock with no trade yet is real data: it has a reference price and an order book, and its
    match price reads 0 at the source. That 0 becomes null, so it is never shown as a price."""
    fields = {key: _number(value) if key in _NUMERIC else value for key, value in fields.items()}
    for key in ("price", "open", "high", "low"):
        if not fields.get(key):
            fields[key] = None
    if not symbol or (fields["price"] is None and fields.get("ref") is None):
        return None
    if fields.get("volume") is None:
        fields["volume"] = 0.0
    return {"symbol": str(symbol), "source": source, **fields}


_NUMERIC = {"price", "volume", "value", "ref", "ceiling", "floor", "open", "high", "low",
            "foreign_buy_volume", "foreign_sell_volume", "last_volume"}


def _vietcap_time(text: Any) -> tuple[float | None, str | None]:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(core.VN_TZ)
    except ValueError:
        return None, None
    return moment.timestamp(), moment.date().isoformat()


def parse_vietcap(payload: Any) -> list[dict[str, Any]]:
    quotes = []
    for row in payload if isinstance(payload, list) else []:
        match, listing, book = row.get("matchPrice") or {}, row.get("listingInfo") or {}, row.get("bidAsk") or {}
        source_time, trading_date = _vietcap_time(match.get("time"))
        bids, asks = book.get("bidPrices") or [], book.get("askPrices") or []
        value = _number(match.get("accumulatedValue"))
        quotes.append(_quote(
            match.get("symbol"), "vietcap", price=match.get("matchPrice"), volume=match.get("accumulatedVolume"),
            value=None if value is None else value * 1e6,  # this feed reports value in millions of VND
            ref=listing.get("refPrice"), ceiling=listing.get("ceiling"), floor=listing.get("floor"),
            open=match.get("openPrice"), high=match.get("highest"), low=match.get("lowest"),
            last_volume=match.get("matchVol"), foreign_buy_volume=match.get("foreignBuyVolume"),
            foreign_sell_volume=match.get("foreignSellVolume"),
            bids=_levels([b.get("price") for b in bids], [b.get("volume") for b in bids]),
            asks=_levels([a.get("price") for a in asks], [a.get("volume") for a in asks]),
            source_time=source_time, trading_date=trading_date,
        ))
    return [q for q in quotes if q]


def _kbs_date(text: Any) -> str | None:
    try:
        return datetime.strptime(str(text), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_kbs(payload: Any) -> list[dict[str, Any]]:
    quotes = []
    for row in payload if isinstance(payload, list) else []:
        quotes.append(_quote(
            row.get("SB"), "kbs", price=row.get("CP"), volume=row.get("TT"), value=row.get("TV"),
            ref=row.get("RE"), ceiling=row.get("CL"), floor=row.get("FL"), open=row.get("OP"),
            high=row.get("HI"), low=row.get("LO"), last_volume=row.get("CV"),
            foreign_buy_volume=row.get("FB"), foreign_sell_volume=row.get("FS"),
            bids=_levels([row.get("B1"), row.get("B2"), row.get("B3")], [row.get("V1"), row.get("V2"), row.get("V3")]),
            asks=_levels([row.get("S1"), row.get("S2"), row.get("S3")], [row.get("U1"), row.get("U2"), row.get("U3")]),
            source_time=None,  # row field `t` is the response time, not the last trade's
            trading_date=_kbs_date(row.get("TD")),
        ))
    return [q for q in quotes if q]


def _chunks(symbols: list[str]) -> list[list[str]]:
    return [symbols[start:start + CHUNK_SIZE] for start in range(0, len(symbols), CHUNK_SIZE)]


class VietcapSource:
    name = "vietcap"
    URL = "https://trading.vietcap.com.vn/api/price/symbols/getList"

    def fetch(self, symbols: list[str]) -> list[dict[str, Any]]:
        rows: list[Any] = []
        for chunk in _chunks(symbols):
            rows += core.fetch_json(self.URL, method="POST", json_body={"symbols": chunk}, headers=core.VIETCAP_HEADERS,
                                    retries=0, timeout=REQUEST_TIMEOUT_S)
        return parse_vietcap(rows)


class KbsSource:
    name = "kbs"
    URL = "https://kbbuddywts.kbsec.com.vn/iis-server/investment/stock/iss"

    def fetch(self, symbols: list[str]) -> list[dict[str, Any]]:
        rows: list[Any] = []
        for chunk in _chunks(symbols):
            rows += core.fetch_json(self.URL, method="POST", json_body={"code": ",".join(chunk)},
                                    headers={"Referer": "https://kbbuddywts.kbsec.com.vn/"},
                                    retries=0, timeout=REQUEST_TIMEOUT_S)
        return parse_kbs(rows)


LIVE_SOURCES: dict[str, Callable[[], QuoteSource]] = {"vietcap": VietcapSource, "kbs": KbsSource}


def build_replay_bars(symbols: list[str], date: str) -> dict[str, Any]:
    """1-minute bars and reference price of one past session, cached under data/replay/."""
    day = datetime.strptime(date, "%Y-%m-%d").date()
    path = core.DATA_DIR / "replay" / f"{day.isoformat()}.json"
    cached: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    end = datetime(day.year, day.month, day.day, 15, 0, tzinfo=core.VN_TZ)
    for symbol in symbols:
        if symbol in cached:
            continue
        data = core.fetch_json(CHART_URL, method="POST", headers=core.VIETCAP_HEADERS, json_body={
            "timeFrame": "ONE_MINUTE", "symbols": [symbol], "to": int(end.timestamp()), "countBack": 300})
        bars = data[0] if data else {}
        rows = [{"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v}
                for t, o, h, l, c, v in zip(*(bars.get(k, []) for k in "tohlcv"))
                if datetime.fromtimestamp(int(t), core.VN_TZ).date() == day]
        if not rows:
            raise core.SourceError(f"Không có nến 1 phút của {symbol} ngày {date}")
        daily = technical.get_prices(symbol, days=30)
        before = daily[daily.index.date < day]
        cached[symbol] = {"ref": float(before["close"].iloc[-1]) if len(before) else None, "bars": rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cached), encoding="utf-8")
    return cached


class ReplaySource:
    """Plays a past session from its 1-minute bars. `speed` is session seconds per real second.

    Bars carry no order book or traded value, so those fields stay empty rather than invented.
    """

    name = "replay"

    def __init__(self, bars: dict[str, Any], date: str, speed: float = 60.0, clock: Callable[[], float] = time.time):
        self.bars, self.date, self.speed, self.clock = bars, date, speed, clock
        self.started = clock()
        day = datetime.strptime(date, "%Y-%m-%d")
        self.session_start = (day.replace(hour=SESSION_START_HOUR_MIN[0], minute=SESSION_START_HOUR_MIN[1],
                                          tzinfo=core.VN_TZ)).timestamp()

    def session_time(self) -> float:
        return self.session_start + (self.clock() - self.started) * self.speed

    def fetch(self, symbols: list[str]) -> list[dict[str, Any]]:
        now = self.session_time()
        quotes = []
        for symbol in symbols:
            entry = self.bars.get(symbol)
            played = [b for b in entry["bars"] if b["t"] <= now] if entry else []
            if not played:
                continue
            quote = _quote(symbol, "replay", price=played[-1]["c"], volume=sum(b["v"] for b in played), value=None,
                           ref=entry["ref"], ceiling=None, floor=None, open=played[0]["o"],
                           high=max(b["h"] for b in played), low=min(b["l"] for b in played),
                           last_volume=played[-1]["v"], foreign_buy_volume=None, foreign_sell_volume=None,
                           bids=[], asks=[], source_time=float(played[-1]["t"]), trading_date=self.date)
            if quote:
                quotes.append(quote)
        return quotes
