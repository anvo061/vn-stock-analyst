"""Command line entry point: python -X utf8 -m stock_analyst.cli <command>.

Every command prints JSON on stdout, so the Claude subagent can read it directly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from datetime import datetime, time as clock_time

from . import core, fundamentals, news, technical
from .collector import HOLIDAYS
from .live_sources import KbsSource, VietcapSource

_ANALYSIS_ERRORS = (core.SourceError, ValueError, KeyError, TypeError, IndexError)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(payload), encoding="utf-8")


def _section(label: str, call: Callable[[], Any], errors: list[str]) -> Any:
    """Run one analysis step; a failure is recorded and leaves the others running."""
    try:
        return call()
    except _ANALYSIS_ERRORS as exc:
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None


def build_brief(symbol: str, hours: int = 72) -> dict[str, Any]:
    """Full data pack for one symbol. A failed section is null and listed in errors."""
    symbol = core.validate_symbol(symbol)
    errors: list[str] = []
    return {
        "symbol": symbol,
        "generated_at": core.now_vn().isoformat(timespec="seconds"),
        "technical": _section("technical", lambda: technical.analyze(symbol), errors),
        "fundamentals": _section("fundamentals", lambda: fundamentals.analyze(symbol), errors),
        "news": _section("news", lambda: news.query(symbol=symbol, hours=hours), errors),
        "errors": errors,
    }


def run_daily(symbols: list[str] | None = None) -> dict[str, Any]:
    """Crawl news, then write one data pack per watchlist symbol to reports/<date>/."""
    today = core.now_vn().date().isoformat()
    out_dir = core.REPORTS_DIR / today
    targets = [core.validate_symbol(s) for s in symbols or core.load_watchlist()["symbols"]]
    crawl_errors: list[str] = []
    crawl = _section("crawl", news.crawl, crawl_errors)

    packs: dict[str, Any] = {}
    for symbol in targets:
        brief = build_brief(symbol)
        path = out_dir / f"{brief['symbol']}.json"
        _write_json(path, brief)
        packs[brief["symbol"]] = {"path": str(path), "errors": brief["errors"]}

    market_news = _section("market_news", lambda: news.query(hours=24, limit=100), crawl_errors)
    market_path = out_dir / "market_news.json"
    _write_json(market_path, market_news or [])

    summary = {
        "date": today,
        "output_dir": str(out_dir),
        "crawl": crawl,
        "crawl_errors": crawl_errors,
        "market_news_path": str(market_path),
        "symbols": packs,
        "ok": not crawl_errors and not any(p["errors"] for p in packs.values()),
    }
    _write_json(out_dir / "summary.json", summary)
    return summary


def _read_report(args: argparse.Namespace) -> dict[str, Any]:
    """Filings are large: save the full extraction, print only its location and shape."""
    report = fundamentals.read_report(args.url, args.symbol, max_pages=args.max_pages)
    out_path = Path(report["path"]).with_suffix(".json")
    _write_json(out_path, report)
    return {
        "extraction_path": str(out_path),
        "path": report["path"],
        "pages": report["pages"],
        "scanned": report["scanned"],
        "tables": len(report["tables"]),
        "warnings": report["warnings"],
    }


TRADING_CHECK_SYMBOLS = ["FPT", "VCB", "HPG", "SSI", "MWG"]  # liquid enough to have traded on any open day
FIRST_MATCH = clock_time(9, 15)  # continuous matching starts here; earlier, no trade proves anything


def _one(positional: str | None, flag: str | None, label: str) -> str:
    """The agents write `--symbol FPT`; people write `FPT`. Both work, conflicting values do not."""
    if positional and flag and positional != flag:
        raise ValueError(f"Hai {label} khác nhau: {positional!r} và {flag!r}")
    value = positional or flag
    if not value:
        raise ValueError(f"Thiếu {label}")
    return value


def is_trading_day(now: datetime | None = None, sources: list[Any] | None = None) -> dict[str, Any]:
    """Weekday, not a listed holiday, and (from 09:15) a price source shows trades dated today.

    When no source can be read the answer is false: a report built on yesterday's data is worse than none.
    """
    now = now or core.now_vn()
    today = now.date().isoformat()
    result: dict[str, Any] = {"date": today, "checked_at": now.isoformat(timespec="seconds"),
                              "checks": {"weekday": now.weekday() < 5, "holiday": today in HOLIDAYS, "price_check": None}}

    def answer(value: bool, reason: str) -> dict[str, Any]:
        return {"is_trading_day": value, **result, "reason": reason}

    if now.weekday() >= 5:
        return answer(False, "Thứ Bảy hoặc Chủ nhật.")
    if today in HOLIDAYS:
        return answer(False, "Ngày nghỉ lễ có trong config/holidays.json.")
    if now.time() < FIRST_MATCH:
        result["checks"]["price_check"] = {"status": "too_early"}
        return answer(True, "Theo lịch là ngày giao dịch; trước 09:15 nên chưa kiểm tra được lệnh khớp.")
    errors = []
    for source in sources if sources is not None else [VietcapSource(), KbsSource()]:
        try:
            quotes = source.fetch(TRADING_CHECK_SYMBOLS)
        except (core.SourceError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{source.name}: {exc}")
            continue
        traded = sorted(q["symbol"] for q in quotes if q.get("trading_date") == today and q.get("price") and q.get("volume"))
        result["checks"]["price_check"] = {"status": "traded" if traded else "no_trades_today", "source": source.name,
                                           "symbols_traded": traded, "symbols_checked": TRADING_CHECK_SYMBOLS}
        if traded:
            return answer(True, f"Có lệnh khớp hôm nay theo {source.name}: {', '.join(traded)}.")
        return answer(False, "Theo lịch là ngày giao dịch nhưng nguồn giá không có lệnh khớp nào hôm nay: "
                             "có thể là ngày nghỉ chưa có trong config/holidays.json.")
    result["checks"]["price_check"] = {"status": "error", "errors": errors}
    return answer(False, "Không kiểm tra được nguồn giá nào; không chạy để tránh dùng dữ liệu cũ.")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("phải là số nguyên dương")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock_analyst", description="Dữ liệu cổ phiếu Việt Nam")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("crawl", help="Cào tin mới từ các nguồn RSS và sitemap")
    p = sub.add_parser("sync-gemini", help="Chạy test, cập nhật STATUS_FOR_GEMINI.md và reports/latest/; --push để commit và đẩy lên GitHub")
    p.add_argument("--step", required=True)
    p.add_argument("--push", action="store_true")

    sub.add_parser("is-trading-day", help="Hôm nay (giờ VN) có phải ngày giao dịch không")
    sub.add_parser("retag", help="Gắn lại mã cho mọi tin đã lưu theo danh sách mã và STOPLIST hiện tại").add_argument("--dry-run", action="store_true")

    p = sub.add_parser("news", help="Truy vấn tin đã cào")
    p.add_argument("--symbol")
    p.add_argument("--keyword")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("article", help="Lấy toàn văn một bài báo")
    p.add_argument("url", nargs="?")
    p.add_argument("--url", dest="url_flag", help="Cách viết khác của URL")

    p = sub.add_parser("fundamentals", help="Phân tích báo cáo tài chính")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--symbol", dest="symbol_flag", help="Cách viết khác của mã")
    p.add_argument("--quarters", type=_positive_int, default=8)

    p = sub.add_parser("reports", help="Liệt kê file báo cáo tài chính PDF")
    p.add_argument("symbol")
    p.add_argument("--year", type=int)

    p = sub.add_parser("read-report", help="Tải và trích xuất một file báo cáo PDF")
    p.add_argument("symbol")
    p.add_argument("url")
    p.add_argument("--max-pages", type=int)

    p = sub.add_parser("render-report", help="Render trang PDF thành ảnh PNG để đọc bản scan")
    p.add_argument("symbol")
    p.add_argument("url")
    p.add_argument("--pages", default="1-5", help='Ví dụ "3-8,12"; tối đa 20 trang mỗi lần')

    p = sub.add_parser("technical", help="Phân tích kỹ thuật")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--symbol", dest="symbol_flag", help="Cách viết khác của mã")
    p.add_argument("--days", type=int, default=500)
    p.add_argument("--no-chart", action="store_true")

    p = sub.add_parser("brief", help="Gói dữ liệu đầy đủ cho một mã")
    p.add_argument("symbol")
    p.add_argument("--hours", type=int, default=72)

    p = sub.add_parser("daily", help="Cào tin và tạo gói dữ liệu cho danh mục theo dõi")
    p.add_argument("--symbols", nargs="*")
    return parser


def _dispatch(args: argparse.Namespace) -> Any:
    command = args.command
    if command == "retag":
        return news.retag(dry_run=args.dry_run)
    if command == "crawl":
        return news.crawl()
    if command == "news":
        return news.query(symbol=args.symbol, hours=args.hours, keyword=args.keyword, limit=args.limit)
    if command == "sync-gemini":
        from . import gemini_sync  # imported late: only this command needs it
        return gemini_sync.sync(args.step, push_after=args.push)
    if command == "is-trading-day":
        return is_trading_day()
    if command == "article":
        return news.fetch_article(_one(args.url, args.url_flag, "URL"))
    if command == "fundamentals":
        return fundamentals.analyze(_one(args.symbol, args.symbol_flag, "mã"), quarters=args.quarters)
    if command == "reports":
        return fundamentals.list_reports(args.symbol, year=args.year)
    if command == "read-report":
        return _read_report(args)
    if command == "render-report":
        return fundamentals.render_report_pages(args.url, args.symbol, pages=args.pages)
    if command == "technical":
        return technical.analyze(_one(args.symbol, args.symbol_flag, "mã"), days=args.days, chart=not args.no_chart)
    if command == "brief":
        return build_brief(args.symbol, hours=args.hours)
    return run_daily(args.symbols)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(_dump(_dispatch(args)))
    except (core.SourceError, ValueError) as exc:
        print(_dump({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
