"""Phase 0 tool: compare live quote sources during a trading session. Read-only and rate limited.

Run: python -X utf8 -m stock_analyst.measure_realtime --minutes 30
Each source is polled by its own thread. A value is identified by (symbol, accumulated volume), so
a source's lag is how long after the first source it showed the same accumulated volume. The fastest
source's own delay behind the exchange cannot be seen this way; `source_age_s` estimates it where
the source publishes an event time.
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from datetime import datetime, time as clock
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from . import core
from .collector import HOLIDAYS

FAST_POLL_S = 3.0
SLOW_POLL_S = 15.0  # whole-board responses are large, so they are asked for less often
MAX_MINUTES = 120
MAX_SYMBOLS = 30
BLOCK_STATUSES = ("HTTP 401", "HTTP 403", "HTTP 429")
SESSIONS = ((clock(9, 0), clock(11, 30)), (clock(13, 0), clock(14, 45)))
PRICE_MISMATCH_TOLERANCE_VND = 1.0


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote(symbol: Any, price: Any, volume: Any, source_time: float | None = None) -> dict[str, Any] | None:
    price, volume = _number(price), _number(volume)
    if not symbol or price is None or volume is None:
        return None
    return {"symbol": str(symbol), "price": price, "volume": volume, "source_time": source_time}


def _iso_epoch(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_vietcap(payload: Any) -> list[dict[str, Any]]:
    quotes = []
    for row in payload if isinstance(payload, list) else []:
        match = row.get("matchPrice") or {}
        quotes.append(_quote(match.get("symbol"), match.get("matchPrice"), match.get("accumulatedVolume"),
                             _iso_epoch(match.get("time"))))
    return [q for q in quotes if q]


def parse_kbs(payload: Any) -> list[dict[str, Any]]:
    # Row field `t` is the response time rather than the last trade's, so no source_time here.
    rows = payload if isinstance(payload, list) else []
    return [q for q in (_quote(r.get("SB"), r.get("CP"), r.get("TT")) for r in rows) if q]


def parse_cafef(payload: Any) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    quotes = []
    for row in rows:
        price = _number(row.get("l"))  # this feed quotes prices in thousands of VND
        quotes.append(_quote(row.get("a"), None if price is None else price * 1000, row.get("totalvolume")))
    return [q for q in quotes if q]


def _sources(symbols: list[str]) -> dict[str, dict[str, Any]]:
    # SSI iBoard stays out: its service terms forbid reproducing site information for third parties.
    return {
        "vietcap": {"interval": FAST_POLL_S, "parse": parse_vietcap, "url": "https://trading.vietcap.com.vn/api/price/symbols/getList",
                    "kwargs": {"method": "POST", "json_body": {"symbols": symbols}, "headers": core.VIETCAP_HEADERS}},
        "kbs": {"interval": FAST_POLL_S, "parse": parse_kbs, "url": "https://kbbuddywts.kbsec.com.vn/iis-server/investment/stock/iss",
                "kwargs": {"method": "POST", "json_body": {"code": ",".join(symbols)},
                           "headers": {"Referer": "https://kbbuddywts.kbsec.com.vn/"}}},
        "cafef": {"interval": SLOW_POLL_S, "parse": parse_cafef, "url": "https://banggia.cafef.vn/stockhandler.ashx",
                  "kwargs": {"params": {"center": 1}}},
    }


def in_session(moment: datetime) -> bool:
    if moment.weekday() >= 5 or moment.date().isoformat() in HOLIDAYS:
        return False
    return any(start <= moment.time() <= end for start, end in SESSIONS)


def is_block(error: str) -> bool:
    return any(status in error for status in BLOCK_STATUSES)


def _poll(name: str, source: dict[str, Any], symbols: set[str], stop_at: float, log: Callable[[dict], None]) -> None:
    fetch_kwargs = {"retries": 0, "timeout": 10, **source["kwargs"]}
    while time.time() < stop_at:
        started = time.time()
        record: dict[str, Any] = {"source": name, "at": started}
        try:
            response = core.fetch(source["url"], **fetch_kwargs)
            payload = response.json()
            record["latency_ms"] = round((time.time() - started) * 1000)
            record["quotes"] = [q for q in source["parse"](payload) if q["symbol"] in symbols]
        except (core.SourceError, ValueError, AttributeError) as exc:
            record["latency_ms"] = round((time.time() - started) * 1000)
            record["error"] = f"{type(exc).__name__}: {exc}"
        log(record)
        if is_block(record.get("error", "")):
            log({"source": name, "at": time.time(), "stopped": record["error"]})
            return  # the source refused us, so do not keep asking
        time.sleep(max(0.0, source["interval"] - (time.time() - started)))


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-source reliability, freshness and agreement. Pure, so it can be tested offline."""
    first_seen: dict[tuple[str, float], dict[str, float]] = {}
    prices: dict[tuple[str, float], dict[str, float]] = {}
    per: dict[str, dict[str, Any]] = {}
    for record in records:
        stats = per.setdefault(record["source"], {"polls": 0, "ok": 0, "errors": {}, "latency": [], "ages": [], "stopped": None})
        if "stopped" in record:
            stats["stopped"] = record["stopped"]
            continue
        stats["polls"] += 1
        stats["latency"].append(record["latency_ms"])
        if "error" in record:
            key = record["error"][:120]
            stats["errors"][key] = stats["errors"].get(key, 0) + 1
            continue
        stats["ok"] += 1
        for quote in record["quotes"]:
            key = (quote["symbol"], quote["volume"])
            first_seen.setdefault(key, {}).setdefault(record["source"], record["at"])
            prices.setdefault(key, {}).setdefault(record["source"], quote["price"])
            if quote["source_time"] is not None:
                stats["ages"].append(record["at"] - quote["source_time"])
    lags: dict[str, list[float]] = {name: [] for name in per}
    for seen in first_seen.values():
        if len(seen) > 1:
            earliest = min(seen.values())
            for name, at in seen.items():
                lags[name].append(at - earliest)
    compared = [p for p in prices.values() if len(p) > 1]
    mismatches = [p for p in compared if max(p.values()) - min(p.values()) > PRICE_MISMATCH_TOLERANCE_VND]
    return {
        "sources": {name: {
            "polls": s["polls"], "ok": s["ok"], "success_rate": round(s["ok"] / s["polls"], 4) if s["polls"] else None,
            "errors": s["errors"], "stopped": s["stopped"],
            "latency_ms_p50": _percentile(s["latency"], 0.5), "latency_ms_p95": _percentile(s["latency"], 0.95),
            "values_compared": len(lags[name]), "lag_s_p50": _percentile(lags[name], 0.5), "lag_s_p95": _percentile(lags[name], 0.95),
            "source_age_s_p50": _percentile(s["ages"], 0.5), "source_age_s_p95": _percentile(s["ages"], 0.95),
        } for name, s in per.items()},
        "values_seen": len(first_seen),
        "values_compared_across_sources": len(compared),
        "price_mismatches": len(mismatches),
        "price_mismatch_samples": mismatches[:5],
    }


def _clock_skew_s() -> float | None:
    """Local clock minus server clock. Event-time ages are only meaningful if this is small."""
    try:
        date = core.fetch("https://trading.vietcap.com.vn/", retries=0, timeout=10).headers.get("Date")
        return round(time.time() - parsedate_to_datetime(date).timestamp(), 1) if date else None
    except (core.SourceError, TypeError, ValueError):
        return None


def run(symbols: list[str], minutes: float, force: bool = False) -> dict[str, Any]:
    started = core.now_vn()
    if not force and not in_session(started):
        raise ValueError(f"Ngoài phiên giao dịch ({started:%a %H:%M}); số đo sẽ vô nghĩa. Dùng --force để chạy thử.")
    if not 0 < minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes phải từ 0 đến {MAX_MINUTES}")
    symbols = [core.validate_symbol(s) for s in symbols][:MAX_SYMBOLS]
    out_dir = core.DATA_DIR / "phase0"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{started:%Y%m%d_%H%M%S}"
    records: list[dict[str, Any]] = []
    lock = threading.Lock()
    raw_path = out_dir / f"measure_{stamp}.jsonl"
    with raw_path.open("w", encoding="utf-8") as raw:
        def log(record: dict[str, Any]) -> None:
            with lock:
                records.append(record)
                raw.write(json.dumps(record, ensure_ascii=False) + "\n")
                raw.flush()

        stop_at = time.time() + minutes * 60
        threads = [threading.Thread(target=_poll, args=(name, source, set(symbols), stop_at, log), daemon=True)
                   for name, source in _sources(symbols).items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    summary = {
        "started": started.isoformat(timespec="seconds"), "minutes": minutes, "symbols": symbols, "in_session": in_session(started),
        "clock_skew_s": _clock_skew_s(), "poll_interval_s": {"vietcap": FAST_POLL_S, "kbs": FAST_POLL_S, "cafef": SLOW_POLL_S},
        "note": "Lag includes each source's poll interval, so sources are only directly comparable at the same interval.",
        "raw_path": str(raw_path), **summarize(records),
    }
    summary_path = out_dir / f"summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Đo độ trễ các nguồn giá trong phiên")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--force", action="store_true", help="Chạy cả ngoài phiên, chỉ để thử công cụ")
    args = parser.parse_args()
    try:
        symbols = args.symbols or list(core.load_watchlist()["symbols"])
        print(json.dumps(run(symbols, args.minutes, args.force), ensure_ascii=False, indent=2))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
