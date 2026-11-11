from datetime import datetime

import pytest

from stock_analyst import core
from stock_analyst import measure_realtime as M


def record(source, at, quotes):
    return {"source": source, "at": at, "latency_ms": 100, "quotes": quotes}


def quote(symbol="FPT", price=50800.0, volume=100.0, source_time=None):
    return {"symbol": symbol, "price": price, "volume": volume, "source_time": source_time}


def test_parsers_share_one_shape_and_skip_incomplete_rows():
    vietcap = M.parse_vietcap([{"matchPrice": {"symbol": "FPT", "matchPrice": 50800, "accumulatedVolume": 12162200,
                                               "time": "2026-11-06T07:45:00.011Z"}}, {"matchPrice": {"symbol": "VCB"}}, {}])
    assert [q["symbol"] for q in vietcap] == ["FPT"] and vietcap[0]["source_time"] is not None
    assert M.parse_kbs([{"SB": "FPT", "CP": 50800, "TT": 12162200}])[0]["price"] == 50800
    assert M.parse_cafef([{"a": "FPT", "l": 50.8, "totalvolume": 12162200}])[0]["price"] == pytest.approx(50800)
    assert M.parse_kbs({"unexpected": 1}) == [] and M.parse_cafef(None) == []


def test_lag_is_measured_from_the_first_source_to_show_a_value():
    result = M.summarize([record("a", 10.0, [quote()]), record("b", 13.0, [quote()]), record("a", 13.5, [quote()])])
    assert result["sources"]["b"]["lag_s_p50"] == 3.0
    assert result["sources"]["a"]["lag_s_p50"] == 0.0  # a's later repeat of the same value is ignored
    assert result["values_compared_across_sources"] == 1 and result["price_mismatches"] == 0


def test_same_volume_with_different_price_is_a_mismatch():
    result = M.summarize([record("a", 1.0, [quote(price=50800.0)]), record("b", 2.0, [quote(price=50900.0)])])
    assert result["price_mismatches"] == 1


def test_errors_and_stops_count_against_success_rate():
    result = M.summarize([
        record("a", 1.0, [quote()]),
        {"source": "a", "at": 2.0, "latency_ms": 5, "error": "SourceError: HTTP 429"},
        {"source": "a", "at": 2.1, "stopped": "SourceError: HTTP 429"},
    ])
    stats = result["sources"]["a"]
    assert stats["polls"] == 2 and stats["success_rate"] == 0.5 and stats["stopped"]
    assert M.is_block("x trả HTTP 403") and not M.is_block("HTTP 500")


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 11, 9, 9, 30), True), (datetime(2026, 11, 9, 12, 0), False),
    (datetime(2026, 11, 9, 14, 45), True), (datetime(2026, 11, 9, 15, 0), False),
    (datetime(2026, 11, 7, 10, 0), False),
])
def test_in_session(moment, expected):
    assert M.in_session(moment) is expected


def test_outside_session_refuses_without_network(monkeypatch):
    monkeypatch.setattr(core, "now_vn", lambda: datetime(2026, 11, 7, 2, 0))
    monkeypatch.setattr(core, "fetch", lambda *a, **k: pytest.fail("no network expected"))
    with pytest.raises(ValueError):
        M.run(["FPT"], 1)


def test_poll_stops_after_being_blocked(monkeypatch):
    calls, logged = [], []

    def blocked(url, **kwargs):
        calls.append(url)
        raise core.SourceError(f"{url} trả HTTP 429")

    monkeypatch.setattr(core, "fetch", blocked)
    source = {"url": "https://x.test", "interval": 0, "parse": M.parse_kbs, "kwargs": {}}
    M._poll("x", source, {"FPT"}, 9e12, logged.append)
    assert len(calls) == 1 and logged[-1]["stopped"]


def test_listed_holiday_is_not_a_session():
    assert M.in_session(datetime(2026, 9, 2, 10, 0)) is False
