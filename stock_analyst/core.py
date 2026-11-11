"""Shared plumbing: HTTP with retry and per-host throttling, paths, symbol validation."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
WATCHLIST_PATH = ROOT / "config" / "watchlist.json"

VN_TZ = timezone(timedelta(hours=7))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
VIETCAP_HEADERS = {
    "Referer": "https://trading.vietcap.com.vn/",
    "Origin": "https://trading.vietcap.com.vn",
}

_MIN_HOST_INTERVAL_S = 0.5
_RETRY_STATUS = {429, 500, 502, 503, 504}
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3}$")
_session = requests.Session()
_host_lock = threading.Lock()
_host_last_call: dict[str, float] = {}


class SourceError(RuntimeError):
    """A data source failed or returned something unusable."""


def validate_symbol(symbol: str) -> str:
    """Normalise a VN ticker; reject anything that is not three alphanumerics."""
    normalised = str(symbol).strip().upper()
    if not _SYMBOL_RE.fullmatch(normalised):
        raise ValueError(f"Mã cổ phiếu không hợp lệ: {symbol!r}")
    return normalised


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    with _host_lock:
        wait = _host_last_call.get(host, 0.0) + _MIN_HOST_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _host_last_call[host] = time.monotonic()


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: float = 25,
    retries: int = 2,
    stream: bool = False,
    allow_redirects: bool = True,
) -> requests.Response:
    """HTTP request with retry on transient failures. Raises SourceError."""
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
    last_error = "unknown"
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(1.5 * attempt)
        _throttle(url)
        try:
            response = _session.request(
                method, url, headers=merged, params=params, json=json_body,
                timeout=timeout, stream=stream, allow_redirects=allow_redirects,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code in _RETRY_STATUS:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code >= 400:
            raise SourceError(f"{url} trả HTTP {response.status_code}")
        return response
    raise SourceError(f"{url} thất bại sau {retries + 1} lần: {last_error}")


def fetch_json(url: str, **kwargs: Any) -> Any:
    response = fetch(url, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"{url} không trả JSON hợp lệ") from exc


def load_watchlist() -> dict[str, Any]:
    """Return {"symbols": {"FPT": {"name": ..., "aliases": [...]}, ...}}."""
    with WATCHLIST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)
