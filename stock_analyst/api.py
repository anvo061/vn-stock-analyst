"""Read-only HTTP API. Viewers poll it; it only ever serves the collector's memory."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import core
from .bars import BarProvider
from .collector import Collector
from .research import FundamentalsCache, NewsReader

REQUESTS_PER_MINUTE = 60  # polling every 3 seconds uses 20
MAX_SYMBOLS_PER_REQUEST = 100


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InviteStore:
    """Per-person invite tokens. Only hashes reach the disk, and revoking applies on the next request."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, invites: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(invites, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def add(self, name: str) -> str:
        """Create an invite for a person and return its token, which cannot be recovered later."""
        name = name.strip()
        if not name:
            raise ValueError("Cần tên người được mời")
        with self._lock:
            invites = self._load()
            if name in invites and not invites[name].get("revoked_at"):
                raise ValueError(f"{name} đã có mã mời còn hiệu lực")
            token = secrets.token_urlsafe(32)
            invites[name] = {"hash": _hash(token), "created_at": core.now_vn().isoformat(timespec="seconds"), "revoked_at": None}
            self._save(invites)
        return token

    def revoke(self, name: str) -> None:
        with self._lock:
            invites = self._load()
            if name not in invites:
                raise ValueError(f"Không có mã mời của {name}")
            invites[name]["revoked_at"] = core.now_vn().isoformat(timespec="seconds")
            self._save(invites)

    def list(self) -> list[dict[str, Any]]:
        return [{"name": name, "created_at": i["created_at"], "revoked_at": i["revoked_at"]} for name, i in self._load().items()]

    def who(self, token: str) -> str | None:
        digest = _hash(token)
        for name, invite in self._load().items():
            if secrets.compare_digest(invite["hash"], digest) and not invite.get("revoked_at"):
                return name
        return None


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time):
        self.per_minute, self.clock = per_minute, clock
        self.calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            calls = self.calls[key]
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= self.per_minute:
                return False
            calls.append(now)
            return True


def create_app(collector: Collector, invites: InviteStore, allowed_origins: list[str],
               limiter: RateLimiter | None = None, lifespan: Any = None, bars: BarProvider | None = None,
               universe: list[dict[str, Any]] | None = None, news_reader: NewsReader | None = None,
               fundamentals: FundamentalsCache | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    limiter = limiter or RateLimiter(REQUESTS_PER_MINUTE)
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # the tunnel is the slow link
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET"], allow_headers=["Authorization"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def viewer(request: Request) -> str:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        name = invites.who(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
        if name is None:
            raise HTTPException(401, "Mã mời không hợp lệ", headers={"WWW-Authenticate": "Bearer"})
        if not limiter.allow(name):
            raise HTTPException(429, "Gọi quá nhanh", headers={"Retry-After": "10"})
        return name

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}  # public, so it says nothing about data or viewers

    @app.get("/api/quotes")
    def quotes(symbols: str | None = Query(None, max_length=500), _: str = Depends(viewer)) -> dict[str, Any]:
        wanted = None
        if symbols:
            try:
                wanted = [core.validate_symbol(s) for s in symbols.split(",") if s.strip()][:MAX_SYMBOLS_PER_REQUEST]
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return collector.snapshot(wanted)

    @app.get("/api/symbols")
    def symbols_list(_: str = Depends(viewer)) -> dict[str, Any]:
        return {"symbols": universe or [], "watchlist": collector.priority}

    @app.get("/api/bars")
    def get_bars(symbol: str = Query(..., max_length=10), interval: str = Query("1D", max_length=4), _: str = Depends(viewer)) -> dict[str, Any]:
        if bars is None:
            raise HTTPException(503, "Chưa bật dữ liệu nến")
        try:
            return bars.get(core.validate_symbol(symbol), interval)
        except KeyError as exc:
            raise HTTPException(404, "Mã không nằm trong danh mục theo dõi") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except core.SourceError as exc:
            raise HTTPException(502, str(exc)) from exc

    def listed(symbol: str) -> str:
        """Only symbols the board knows, so a viewer cannot make the server fetch arbitrary codes."""
        try:
            symbol = core.validate_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if symbol not in collector.known:
            raise HTTPException(404, "Mã không nằm trong danh sách niêm yết")
        return symbol

    @app.get("/api/news")
    def get_news(symbol: str | None = Query(None, max_length=10), hours: int = Query(72), limit: int = Query(30),
                 _: str = Depends(viewer)) -> dict[str, Any]:
        if news_reader is None:
            raise HTTPException(503, "Chưa bật dữ liệu tin")
        try:
            return news_reader.get(listed(symbol) if symbol else None, hours=hours, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/fundamentals")
    def get_fundamentals(symbol: str = Query(..., max_length=10), _: str = Depends(viewer)) -> dict[str, Any]:
        if fundamentals is None:
            raise HTTPException(503, "Chưa bật dữ liệu BCTC")
        try:
            return fundamentals.get(listed(symbol))
        except core.SourceError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/status")
    def status(_: str = Depends(viewer)) -> dict[str, Any]:
        return collector.status()

    return app
