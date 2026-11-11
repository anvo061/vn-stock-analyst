"""One background collector shared by all viewers: polls a source, fails over, keeps the latest quotes."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable

from . import core
from .live_sources import QuoteSource

ACTIVE_POLL_S = 3.0
CLOSED_POLL_S = 60.0
FAILURES_BEFORE_SWITCH = 3
BLOCK_COOLDOWN_S = 15 * 60
PRIMARY_PROBE_S = 60.0
FROZEN_AFTER_S = 90.0  # HTTP 200 with nothing moving during continuous trading counts as a failure
STALE_ACTIVE_S = 15.0
STALE_CLOSED_S = 180.0
BLOCK_MARKERS = ("HTTP 401", "HTTP 403", "HTTP 429")
PRICE_TOLERANCE_VND = 1.0
RECORD_RETENTION_DAYS = 30
ROTATION_SIZE = 250  # with one call every 3 seconds, the whole market of about 1,500 turns over in under 20 seconds
MAX_PRIORITY = 100
VIEW_TTL_S = 30.0  # a symbol stays in the fast tier this long after someone last asked for it

_SCHEDULE = (
    (clock_time(8, 30), "pre_open"), (clock_time(9, 0), "ato"), (clock_time(9, 15), "morning"),
    (clock_time(11, 30), "lunch"), (clock_time(13, 0), "afternoon"), (clock_time(14, 30), "atc"),
    (clock_time(14, 45), "post"), (clock_time(15, 0), "closed"),
)
_CONTINUOUS = {"morning", "afternoon"}
_QUIET = {"closed", "holiday"}  # no trading, so slow polling and a longer staleness limit


HOLIDAYS_PATH = core.ROOT / "config" / "holidays.json"
HOLIDAY_CALENDAR_WARN_DAYS = 45


def load_holidays(path: Path = HOLIDAYS_PATH) -> tuple[frozenset[str], str | None]:
    """Exchange holidays as ISO dates, plus the last date the list covers. A missing file means none known."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(), None
    dates = {*data.get("observed", {}).get("dates", []), *(entry["date"] for entry in data.get("announced", []))}
    return frozenset(dates), data.get("covers_until")


HOLIDAYS, HOLIDAYS_UNTIL = load_holidays()


def market_state(moment: datetime, holidays: frozenset[str] | None = None) -> str:
    """HOSE schedule. Days after the holiday calendar ends read as normal weekdays, so keep it current."""
    if moment.weekday() >= 5:
        return "closed"
    if moment.date().isoformat() in (HOLIDAYS if holidays is None else holidays):
        return "holiday"
    state = "closed"
    for start, name in _SCHEDULE:
        if moment.time() >= start:
            state = name
    return state


class QuoteRecorder:
    """Keeps every changed quote for 30 days, so a whole session can be checked afterwards."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS quote_log (at REAL, symbol TEXT, source TEXT, price REAL, volume REAL, trading_date TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS quote_log_at ON quote_log (at)")
        self.db.execute("DELETE FROM quote_log WHERE at < ?", (time.time() - RECORD_RETENTION_DAYS * 86400,))
        self.db.commit()

    def write(self, at: float, quotes: list[dict[str, Any]]) -> None:
        self.db.executemany("INSERT INTO quote_log VALUES (?, ?, ?, ?, ?, ?)",
                            [(at, q["symbol"], q["source"], q["price"], q["volume"], q.get("trading_date")) for q in quotes])
        self.db.commit()

    def session_report(self, date: str) -> dict[str, Any]:
        """How continuously one day's quotes arrived. Gaps count only within continuous trading."""
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=core.VN_TZ)
        rows = self.db.execute("SELECT at, source FROM quote_log WHERE at >= ? AND at < ? ORDER BY at",
                               (day.timestamp(), (day + timedelta(days=1)).timestamp())).fetchall()
        iso = lambda at: datetime.fromtimestamp(at, core.VN_TZ).isoformat(timespec="seconds")
        by_source: dict[str, int] = {}
        for _, source in rows:
            by_source[source] = by_source.get(source, 0) + 1
        gap, gap_at = 0.0, None
        for (before, _), (after, _) in zip(rows, rows[1:]):
            same_state = market_state(datetime.fromtimestamp(before, core.VN_TZ)) == market_state(datetime.fromtimestamp(after, core.VN_TZ))
            if same_state and market_state(datetime.fromtimestamp(before, core.VN_TZ)) in _CONTINUOUS and after - before > gap:
                gap, gap_at = after - before, before
        return {"date": date, "updates": len(rows), "by_source": by_source, "first": iso(rows[0][0]) if rows else None,
                "last": iso(rows[-1][0]) if rows else None, "largest_gap_s": round(gap, 1), "largest_gap_at": iso(gap_at) if gap_at else None}

    def close(self) -> None:
        self.db.close()


class Collector:
    """Every poll is one source call carrying the fast tier (the watchlist and whatever viewers are
    looking at) plus the next slice of the rest of the market. The first poll carries everything."""

    def __init__(self, sources: list[QuoteSource], symbols: list[str], *, priority: list[str] | None = None, mode: str = "live",
                 clock: Callable[[], float] = time.time, recorder: QuoteRecorder | None = None):
        if not sources:
            raise ValueError("Cần ít nhất một nguồn giá")
        self.sources, self.symbols, self.mode, self.clock, self.recorder = sources, symbols, mode, clock, recorder
        self.known = set(symbols)
        self.priority = list(symbols if priority is None else priority)
        self.recorded = set(self.priority)  # the whole market would add millions of rows a day
        self.viewed: dict[str, float] = {}
        self.cursor = 0
        self.swept = False
        self.active = 0
        self.failures = 0
        self.cooldown_until: dict[int, float] = {}
        self.next_probe = 0.0
        self.quotes: dict[str, dict[str, Any]] = {}
        self.version = 0
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.last_movement: float | None = None
        self.rejected = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self.stats = {s.name: {"polls": 0, "ok": 0, "errors": 0} for s in sources}
        self._lock = threading.Lock()

    # ----- polling -----

    def _event(self, now: float, message: str) -> None:
        if self.events and self.events[-1]["message"] == message:
            return  # a condition that persists is logged once, not every tick
        self.events.append({"at": datetime.fromtimestamp(now, core.VN_TZ).isoformat(timespec="seconds"), "message": message})

    def _usable(self, index: int, now: float) -> bool:
        return self.cooldown_until.get(index, 0.0) <= now

    def _switch_away(self, now: float, reason: str) -> None:
        """Move to the next source not cooling down. Stays put if there is none."""
        for step in range(1, len(self.sources)):
            candidate = (self.active + step) % len(self.sources)
            if self._usable(candidate, now):
                self._event(now, f"Chuyển từ {self.sources[self.active].name} sang {self.sources[candidate].name}: {reason}")
                self.active, self.failures, self.last_movement = candidate, 0, now
                self.next_probe = now + PRIMARY_PROBE_S
                return
        self._event(now, f"Không còn nguồn thay thế cho {self.sources[self.active].name}: {reason}")

    def _batch(self, now: float) -> list[str]:
        """The symbols for this call: everything until one full sweep succeeds, then fast tier plus a slice."""
        if not self.swept:
            return list(self.symbols)
        with self._lock:
            self.viewed = {symbol: at for symbol, at in self.viewed.items() if now - at <= VIEW_TTL_S}
            viewed = sorted(self.viewed, key=self.viewed.get, reverse=True)
        fast = list(dict.fromkeys([*self.priority, *viewed]))[:MAX_PRIORITY]
        taken = set(fast)
        others = [symbol for symbol in self.symbols if symbol not in taken]
        if not others:
            return fast
        start = self.cursor % len(others)
        slice_ = (others[start:] + others[:start])[:ROTATION_SIZE]
        self.cursor = start + len(slice_)
        return fast + slice_

    def _accept(self, quote: dict[str, Any]) -> bool:
        """Reject impossible prices, and data older than what is already held (a source behind after failover)."""
        price, ceiling, floor = quote["price"], quote.get("ceiling"), quote.get("floor")
        if quote["volume"] < 0 or (price is not None and price <= 0):
            return False
        if price is not None and ceiling and floor and not floor - PRICE_TOLERANCE_VND <= price <= ceiling + PRICE_TOLERANCE_VND:
            return False
        held = self.quotes.get(quote["symbol"])
        if held and held.get("trading_date") and quote.get("trading_date"):
            if quote["trading_date"] < held["trading_date"]:
                return False
            if quote["trading_date"] == held["trading_date"]:
                if quote["volume"] < held["volume"] or (price is None and held["price"] is not None):
                    return False  # a trade cannot un-happen within a day
        return True

    def _merge(self, quotes: list[dict[str, Any]], now: float) -> None:
        changed, moved = [], False
        with self._lock:
            for quote in quotes:
                if quote["symbol"] not in self.known:
                    continue
                if not self._accept(quote):
                    self.rejected += 1
                    continue
                held = self.quotes.get(quote["symbol"])
                if held is None or any(held.get(k) != v for k, v in quote.items()):
                    changed.append(quote)
                    moved = moved or held is None or held["volume"] != quote["volume"]
                self.quotes[quote["symbol"]] = {**quote, "received_at": now}
            if changed:
                self.version += 1
            self.last_success = now
            if moved or self.last_movement is None:
                self.last_movement = now
        recorded = [quote for quote in changed if quote["symbol"] in self.recorded]
        if recorded and self.recorder:
            self.recorder.write(now, recorded)

    def _poll(self, index: int, now: float, batch: list[str]) -> bool:
        source = self.sources[index]
        stats = self.stats[source.name]
        stats["polls"] += 1
        try:
            quotes = source.fetch(batch)
            if not quotes:
                raise core.SourceError(f"{source.name} không trả mã nào")
        except (core.SourceError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            stats["errors"] += 1
            self.last_error = f"{source.name}: {type(exc).__name__}: {exc}"[:300]
            if any(marker in str(exc) for marker in BLOCK_MARKERS):
                self.cooldown_until[index] = now + BLOCK_COOLDOWN_S
                self._event(now, f"{source.name} từ chối truy cập, tạm ngừng gọi {BLOCK_COOLDOWN_S // 60} phút")
                if index == self.active:
                    self._switch_away(now, "bị từ chối truy cập")
            elif index == self.active:
                self.failures += 1
                if self.failures >= FAILURES_BEFORE_SWITCH:
                    self._switch_away(now, f"{self.failures} lần lỗi liên tiếp")
            return False
        stats["ok"] += 1
        self._merge(quotes, now)
        self.swept = True  # the only call before this flag is set carries the whole market
        return True

    def tick(self) -> float:
        """One polling step. Returns the seconds to wait before the next."""
        now = self.clock()
        state = market_state(datetime.fromtimestamp(now, core.VN_TZ))
        wait = CLOSED_POLL_S if state in _QUIET and self.mode == "live" else ACTIVE_POLL_S
        batch = self._batch(now)

        if self.active != 0 and now >= self.next_probe and self._usable(0, now):
            self.next_probe = now + PRIMARY_PROBE_S
            if self._poll(0, now, batch):
                self._event(now, f"Quay lại nguồn chính {self.sources[0].name}")
                self.active, self.failures, self.last_movement = 0, 0, now
                return wait

        if not self._usable(self.active, now):
            self._switch_away(now, "đang tạm ngừng")
        if self._usable(self.active, now) and self._poll(self.active, now, batch):
            self.failures = 0
            frozen = self.last_movement is not None and now - self.last_movement > FROZEN_AFTER_S
            if frozen and state in _CONTINUOUS and self.mode == "live":
                self.last_movement = now
                self._event(now, f"{self.sources[self.active].name} không có lệnh khớp mới sau {FROZEN_AFTER_S:.0f} giây trong phiên")
                self._switch_away(now, "dữ liệu đứng yên")
        return wait

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            stop.wait(self.tick())

    # ----- reading -----

    def feed_status(self) -> dict[str, Any]:
        now = self.clock()
        state = market_state(datetime.fromtimestamp(now, core.VN_TZ))
        limit = STALE_CLOSED_S if state in _QUIET else STALE_ACTIVE_S
        age = None if self.last_success is None else round(now - self.last_success, 1)
        status = "starting" if age is None and self.stats[self.sources[self.active].name]["polls"] == 0 else (
            "offline" if age is None or age > limit else "ok")
        return {
            "status": status, "mode": self.mode, "market": state, "source": self.sources[self.active].name,
            "primary": self.sources[0].name, "on_primary": self.active == 0, "last_success_age_s": age,
            "server_time": datetime.fromtimestamp(now, core.VN_TZ).isoformat(timespec="seconds"),
        }

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Quotes for the requested symbols, which then join the fast tier. No list means the watchlist."""
        wanted = symbols or self.priority
        now = self.clock()
        with self._lock:
            if symbols:
                self.viewed.update({s: now for s in symbols if s in self.known})
            quotes = [dict(self.quotes[s]) for s in wanted if s in self.quotes]
            version = self.version
        for quote in quotes:
            quote["age_s"] = round(now - quote.pop("received_at"), 1)
            ref, price = quote.get("ref"), quote["price"]
            quote["change"] = None if not ref or price is None else round(price - ref, 2)
            quote["change_pct"] = None if not ref or price is None else round((price - ref) / ref * 100, 2)
        held = {q["symbol"] for q in quotes}
        return {"feed": self.feed_status(), "version": version, "quotes": quotes,
                "missing": [s for s in wanted if s not in held]}

    def status(self) -> dict[str, Any]:
        today = datetime.fromtimestamp(self.clock(), core.VN_TZ).date()
        with self._lock:
            return {"feed": self.feed_status(), "symbols": len(self.symbols), "quotes_held": len(self.quotes),
                    "viewing": len(self.viewed), "last_error": self.last_error, "rejected_quotes": self.rejected,
                    "sources": self.stats, "events": list(self.events),
                    "holiday_calendar": {"covers_until": HOLIDAYS_UNTIL, "warning": holiday_calendar_warning(today, HOLIDAYS_UNTIL)}}


def holiday_calendar_warning(today: Any, covers_until: str | None) -> str | None:
    """Speak up before the calendar runs out, since an unlisted holiday looks like a frozen feed."""
    if not covers_until:
        return "Chưa có lịch nghỉ lễ (config/holidays.json); ngày lễ sẽ bị coi là ngày giao dịch không có lệnh khớp."
    end = datetime.strptime(covers_until, "%Y-%m-%d").date()
    if (end - today).days <= HOLIDAY_CALENDAR_WARN_DAYS:
        return f"Lịch nghỉ lễ chỉ đến {covers_until}; thêm lịch nghỉ HOSE mới công bố (nhất là Tết) vào config/holidays.json."
    return None
