"""Run the live backend and manage invites.

python -X utf8 -m stock_analyst.serve run                                 live quotes
python -X utf8 -m stock_analyst.serve run --replay 2026-11-06 --speed 60  replay a past session
python -X utf8 -m stock_analyst.serve report --date 2026-11-09            how continuous a session was
python -X utf8 -m stock_analyst.serve invite add "Tên" [--web URL] [--api URL]   prints a token once (and the full link)
python -X utf8 -m stock_analyst.serve invite link --token TOKEN [--web URL] [--api URL]   link for an existing token
python -X utf8 -m stock_analyst.serve invite list
python -X utf8 -m stock_analyst.serve invite revoke "Tên"

The server binds to 127.0.0.1 only. A tunnel is the only way in from outside.
`--web` defaults to "web_url" in config/live.json when set there.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import uvicorn

from . import core
from .api import InviteStore, create_app
from .bars import BarProvider
from .collector import HOLIDAYS_UNTIL, Collector, QuoteRecorder, holiday_calendar_warning
from .live_sources import LIVE_SOURCES, ReplaySource, build_replay_bars, load_universe
from .research import FundamentalsCache, NewsReader

CONFIG_PATH = core.ROOT / "config" / "live.json"
BIND_HOST = "127.0.0.1"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def invite_store() -> InviteStore:
    return InviteStore(core.DATA_DIR / "live" / "invites.json")


_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _checked_url(value: str, label: str) -> Any:
    """https anywhere, plain http only on this machine: the same rule the web app enforces."""
    parts = urlsplit(str(value).strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{label} không hợp lệ: {value!r}")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise ValueError(f"{label} phải dùng https: {value!r}")
    if parts.fragment:
        raise ValueError(f"{label} không được có phần sau dấu #: {value!r}")
    return parts


def build_invite_link(web: str, token: str, api: str | None = None) -> str:
    """<web>#api=<backend origin>&token=<token>. The part after # never reaches the web host's server.

    Without `api`, the app calls /api on its own origin (local development through Vite's proxy).
    """
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ValueError("Mã mời không đúng dạng")
    page = _checked_url(web, "Địa chỉ web")
    web_url = urlunsplit((page.scheme, page.netloc, page.path or "/", page.query, ""))
    fragment = []
    if api:
        backend = _checked_url(api, "Địa chỉ máy chủ")
        if backend.path not in ("", "/") or backend.query:
            raise ValueError(f"Địa chỉ máy chủ chỉ gồm giao thức và tên miền, ví dụ https://abc.trycloudflare.com: {api!r}")
        fragment.append("api=" + quote(f"{backend.scheme}://{backend.netloc}", safe=""))
    fragment.append("token=" + token)
    return f"{web_url}#{'&'.join(fragment)}"


def _web_url(args: argparse.Namespace) -> str | None:
    return args.web or load_config().get("web_url") or None


def build_collector(config: dict[str, Any], replay: str | None, speed: float) -> tuple[Collector, list[dict[str, Any]]]:
    watchlist = [core.validate_symbol(s) for s in core.load_watchlist()["symbols"]]
    universe = load_universe()
    if replay:
        # A replay stays small: it needs a day of bars per symbol, so only the watchlist is played.
        source = ReplaySource(build_replay_bars(watchlist, replay), replay, speed)
        return Collector([source], watchlist, mode="replay"), [e for e in universe if e["symbol"] in set(watchlist)]
    unknown = [name for name in config["sources"] if name not in LIVE_SOURCES]
    if unknown:
        raise ValueError(f"Nguồn không hỗ trợ: {unknown}; có {sorted(LIVE_SOURCES)}")
    symbols = list(dict.fromkeys([*watchlist, *(entry["symbol"] for entry in universe)]))
    collector = Collector([LIVE_SOURCES[name]() for name in config["sources"]], symbols, priority=watchlist,
                          recorder=QuoteRecorder(core.DATA_DIR / "live" / "quotes.db"))
    return collector, universe


def run_server(args: argparse.Namespace) -> None:
    config = load_config()
    collector, universe = build_collector(config, args.replay, args.speed)
    calendar_warning = holiday_calendar_warning(core.now_vn().date(), HOLIDAYS_UNTIL)
    if calendar_warning:
        print(f"Cảnh báo: {calendar_warning}", file=sys.stderr)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        worker = threading.Thread(target=collector.run, args=(stop,), daemon=True)
        worker.start()
        yield
        stop.set()
        worker.join(timeout=10)
        if collector.recorder:
            collector.recorder.close()

    replay = collector.sources[0] if isinstance(collector.sources[0], ReplaySource) else None
    app = create_app(collector, invite_store(), config["allowed_origins"], lifespan=lifespan,
                     bars=BarProvider(collector.symbols, replay), universe=universe,
                     news_reader=NewsReader(), fundamentals=FundamentalsCache())
    uvicorn.run(app, host=BIND_HOST, port=config["port"], log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock_analyst.serve")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--replay", help="Ngày phát lại, YYYY-MM-DD")
    run.add_argument("--speed", type=float, default=60.0)
    sub.add_parser("report").add_argument("--date", help="YYYY-MM-DD, mặc định hôm nay")
    invite = sub.add_parser("invite")
    action = invite.add_subparsers(dest="action", required=True)
    action.add_parser("list")
    action.add_parser("revoke").add_argument("name")
    add = action.add_parser("add")
    add.add_argument("name")
    link = action.add_parser("link", help="Ghép link cho một mã mời còn hiệu lực")
    link.add_argument("--token", required=True)
    for each in (add, link):
        each.add_argument("--web", help="Địa chỉ giao diện, ví dụ https://ten.github.io/bang-dien/")
        each.add_argument("--api", help="Địa chỉ backend qua tunnel, ví dụ https://abc.trycloudflare.com")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run_server(args)
        elif args.command == "report":
            recorder = QuoteRecorder(core.DATA_DIR / "live" / "quotes.db")
            print(json.dumps(recorder.session_report(args.date or core.now_vn().date().isoformat()), ensure_ascii=False, indent=2))
            recorder.close()
        elif args.action == "add":
            web = _web_url(args)
            if web:  # check the addresses before creating anything, so a typo leaves no orphan invite
                build_invite_link(web, "x" * 43, args.api)
            token = invite_store().add(args.name)
            result = {"name": args.name, "token": token, "note": "Mã chỉ hiện một lần."}
            if web:
                result["link"] = build_invite_link(web, token, args.api)
            print(json.dumps(result, ensure_ascii=False))
        elif args.action == "link":
            web = _web_url(args)
            if not web:
                raise ValueError('Cần --web, hoặc đặt "web_url" trong config/live.json')
            if invite_store().who(args.token) is None:
                raise ValueError("Mã mời không đúng hoặc đã bị thu hồi")
            print(json.dumps({"link": build_invite_link(web, args.token, args.api)}, ensure_ascii=False))
        elif args.action == "revoke":
            invite_store().revoke(args.name)
            print(json.dumps({"revoked": args.name}, ensure_ascii=False))
        else:
            print(json.dumps(invite_store().list(), ensure_ascii=False, indent=2))
    except (ValueError, core.SourceError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
