"""Candles for the chart, cached so viewers never multiply calls to the source."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import core, technical
from .live_sources import CHART_URL, ReplaySource

INTERVALS = ("1D", "1m")
DAILY_SESSIONS = 500
INTRADAY_BARS = 300
TTL_S = {"1D": 600.0, "1m": 20.0}
MAX_CACHED = 200  # with every listed stock available, the cache must not grow without limit


def _rows_from_chart(data: Any) -> list[dict[str, Any]]:
    bars = data[0] if data else {}
    return [{"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for t, o, h, l, c, v in zip(*(bars.get(k, []) for k in "tohlcv"))]


def _rows_from_daily(frame: Any) -> list[dict[str, Any]]:
    return [{"time": day.strftime("%Y-%m-%d"), "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
             "close": float(row["close"]), "volume": float(row["volume"])} for day, row in frame.iterrows()]


class BarProvider:
    def __init__(self, symbols: list[str], replay: ReplaySource | None = None, clock: Callable[[], float] = time.time):
        self.symbols, self.replay, self.clock = set(symbols), replay, clock
        self.cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _fetch_live(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        if interval == "1D":
            return _rows_from_daily(technical.get_prices(symbol, days=DAILY_SESSIONS))
        return _rows_from_chart(core.fetch_json(CHART_URL, method="POST", headers=core.VIETCAP_HEADERS, retries=0, timeout=10, json_body={
            "timeFrame": "ONE_MINUTE", "symbols": [symbol], "to": int(self.clock()), "countBack": INTRADAY_BARS}))

    def _fetch_replay(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        """A replay shows nothing after its own session clock, so it cannot reveal the outcome."""
        if interval == "1D":
            day = datetime.strptime(self.replay.date, "%Y-%m-%d").date()
            frame = technical.get_prices(symbol, days=DAILY_SESSIONS)
            return _rows_from_daily(frame[frame.index.date < day])
        now = self.replay.session_time()
        rows = (self.replay.bars.get(symbol) or {}).get("bars", [])
        return [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]} for b in rows if b["t"] <= now]

    def get(self, symbol: str, interval: str) -> dict[str, Any]:
        if symbol not in self.symbols:
            raise KeyError(symbol)
        if interval not in INTERVALS:
            raise ValueError(f"interval phải là một trong {INTERVALS}")
        now = self.clock()
        key = (symbol, interval)
        ttl = 0.0 if self.replay and interval == "1m" else TTL_S[interval]
        with self._lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            warnings: list[str] = []
            try:
                rows = self._fetch_replay(symbol, interval) if self.replay else self._fetch_live(symbol, interval)
            except (core.SourceError, ValueError, KeyError, IndexError):
                if not hit:
                    raise core.SourceError(f"Không lấy được nến {interval} của {symbol}") from None
                return {**hit[1], "warnings": [*hit[1]["warnings"], "Nguồn nến đang lỗi, đây là dữ liệu đã lưu trước đó."]}
            if interval == "1D":
                warnings.append("Giá đã điều chỉnh cổ tức và chia tách, không phải giá khớp thực tế.")
            result = {"symbol": symbol, "interval": interval, "bars": rows, "warnings": warnings,
                      "mode": "replay" if self.replay else "live"}
            self.cache.pop(key, None)
            self.cache[key] = (now, result)
            if len(self.cache) > MAX_CACHED:
                self.cache.pop(next(iter(self.cache)))
            return result
