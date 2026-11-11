import json

import pytest

from stock_analyst import cli, core, fundamentals, news, technical


def _boom(*args, **kwargs):
    raise core.SourceError("nguồn lỗi")


def _patch_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(core, "load_watchlist", lambda: {"symbols": {"FPT": {}, "VCB": {}}})
    monkeypatch.setattr(news, "crawl", lambda: {"new": 1, "total": 1, "sources": [], "warnings": []})
    monkeypatch.setattr(news, "query", lambda **kwargs: [])
    monkeypatch.setattr(technical, "analyze", lambda symbol: {"symbol": symbol})
    monkeypatch.setattr(fundamentals, "analyze", lambda symbol: {"symbol": symbol})


def test_daily_writes_one_pack_per_symbol(monkeypatch, tmp_path):
    _patch_ok(monkeypatch, tmp_path)
    summary = cli.run_daily()
    assert summary["ok"] is True and set(summary["symbols"]) == {"FPT", "VCB"}
    pack = json.loads(open(summary["symbols"]["FPT"]["path"], encoding="utf-8").read())
    assert pack["technical"] == {"symbol": "FPT"} and pack["errors"] == []


def test_failed_section_is_reported_and_does_not_stop_the_run(monkeypatch, tmp_path):
    _patch_ok(monkeypatch, tmp_path)
    monkeypatch.setattr(fundamentals, "analyze", _boom)
    monkeypatch.setattr(news, "crawl", _boom)
    summary = cli.run_daily()
    assert summary["ok"] is False and summary["crawl"] is None and summary["crawl_errors"]
    assert "fundamentals" in summary["symbols"]["VCB"]["errors"][0]
    pack = json.loads(open(summary["symbols"]["VCB"]["path"], encoding="utf-8").read())
    assert pack["fundamentals"] is None and pack["technical"] == {"symbol": "VCB"}


def test_daily_rejects_bad_symbol_before_crawling(monkeypatch, tmp_path):
    _patch_ok(monkeypatch, tmp_path)
    monkeypatch.setattr(news, "crawl", _boom)
    with pytest.raises(ValueError):
        cli.run_daily(["../x"])


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_quarters_must_be_positive(value):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["fundamentals", "FPT", "--quarters", value])


def test_main_prints_json_error_and_nonzero_exit(capsys):
    assert cli.main(["technical", "../etc"]) == 1
    assert "không hợp lệ" in json.loads(capsys.readouterr().out)["error"]


# --- is-trading-day and flag syntax (11/11/2026) ----------------------------------------

from datetime import datetime  # noqa: E402

from stock_analyst import core as _core  # noqa: E402


class FakeSource:
    def __init__(self, name, quotes=None, error=None):
        self.name, self.quotes, self.error, self.calls = name, quotes or [], error, 0

    def fetch(self, symbols):
        self.calls += 1
        if self.error:
            raise self.error
        return self.quotes


def vn(*args):
    return datetime(*args, tzinfo=_core.VN_TZ)


def traded(day="2026-11-11"):
    return [{"symbol": "FPT", "price": 50900.0, "volume": 100.0, "trading_date": day}]


def test_weekend_and_listed_holiday_are_not_trading_days():
    source = FakeSource("v", traded())
    saturday = cli.is_trading_day(vn(2026, 11, 14, 15, 0), [source])
    assert saturday["is_trading_day"] is False and "Thứ Bảy" in saturday["reason"]
    holiday = cli.is_trading_day(vn(2026, 9, 2, 15, 0), [source])
    assert holiday["is_trading_day"] is False and holiday["checks"]["holiday"] is True
    assert source.calls == 0  # no network needed to say no


def test_trades_dated_today_confirm_a_trading_day():
    result = cli.is_trading_day(vn(2026, 11, 11, 15, 0), [FakeSource("v", traded())])
    assert result["is_trading_day"] is True and result["date"] == "2026-11-11"
    assert result["checks"]["price_check"]["symbols_traded"] == ["FPT"]
    json.dumps(result, allow_nan=False)


def test_no_trades_today_catches_an_unlisted_holiday():
    stale = cli.is_trading_day(vn(2026, 11, 11, 15, 0), [FakeSource("v", traded("2026-11-10"))])
    assert stale["is_trading_day"] is False and "holidays.json" in stale["reason"]
    empty_price = [{"symbol": "FPT", "price": None, "volume": 0.0, "trading_date": "2026-11-11"}]
    assert cli.is_trading_day(vn(2026, 11, 11, 15, 0), [FakeSource("v", empty_price)])["is_trading_day"] is False


def test_fallback_source_and_total_failure():
    broken, backup = FakeSource("v", error=_core.SourceError("HTTP 500")), FakeSource("k", traded())
    result = cli.is_trading_day(vn(2026, 11, 11, 15, 0), [broken, backup])
    assert result["is_trading_day"] is True and result["checks"]["price_check"]["source"] == "k"
    failed = cli.is_trading_day(vn(2026, 11, 11, 15, 0), [broken, FakeSource("k", error=_core.SourceError("down"))])
    assert failed["is_trading_day"] is False and failed["checks"]["price_check"]["status"] == "error"


def test_before_the_first_match_the_calendar_decides():
    source = FakeSource("v", [])
    early = cli.is_trading_day(vn(2026, 11, 11, 8, 30), [source])
    assert early["is_trading_day"] is True and early["checks"]["price_check"]["status"] == "too_early" and source.calls == 0


def test_symbol_and_url_flags_match_positional_arguments(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(technical, "analyze", lambda symbol, days, chart: seen.append((symbol, days, chart)) or {"ok": True})
    monkeypatch.setattr(fundamentals, "analyze", lambda symbol, quarters: seen.append((symbol, quarters)) or {"ok": True})
    monkeypatch.setattr(news, "fetch_article", lambda url: seen.append(url) or {"ok": True})
    assert cli.main(["technical", "--symbol", "FPT", "--days", "300", "--no-chart"]) == 0
    assert cli.main(["technical", "FPT", "--no-chart"]) == 0
    assert cli.main(["fundamentals", "--symbol", "VCB"]) == 0
    assert cli.main(["article", "--url", "https://cafef.vn/a.chn"]) == 0
    assert seen == [("FPT", 300, False), ("FPT", 500, False), ("VCB", 8), "https://cafef.vn/a.chn"]
    capsys.readouterr()
    assert cli.main(["technical", "FPT", "--symbol", "VCB"]) == 1
    assert "khác nhau" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["technical"]) == 1
