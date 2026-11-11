import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from stock_analyst import collector as C
from stock_analyst import core, live_sources as L
from stock_analyst.api import InviteStore, RateLimiter, create_app
from stock_analyst.bars import BarProvider

MONDAY_10AM = datetime(2026, 11, 9, 10, 0, tzinfo=core.VN_TZ).timestamp()
SATURDAY = datetime(2026, 11, 7, 10, 0, tzinfo=core.VN_TZ).timestamp()


def quote(symbol="FPT", price=50800.0, volume=100.0, source="a", date="2026-11-09", **extra):
    return {"symbol": symbol, "source": source, "price": price, "volume": volume, "ref": 50000.0, "ceiling": 53500.0,
            "floor": 46500.0, "trading_date": date, **extra}


class Clock:
    def __init__(self, now=MONDAY_10AM):
        self.now = now

    def __call__(self):
        return self.now


class Fake:
    def __init__(self, name):
        self.name, self.calls, self.result = name, 0, None
        self.volume = 100.0

    def fetch(self, symbols):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        self.volume += 100
        return [quote(volume=self.volume, source=self.name)]


def make(clock=None, mode="live"):
    a, b = Fake("a"), Fake("b")
    return C.Collector([a, b], ["FPT"], clock=clock or Clock(), mode=mode), a, b


# --- adapters ---------------------------------------------------------------------------

VIETCAP_ROW = {"listingInfo": {"refPrice": 52900, "ceiling": 56600, "floor": 49200},
               "matchPrice": {"symbol": "FPT", "matchPrice": 50800, "accumulatedVolume": 12162200, "accumulatedValue": 622449.45,
                              "openPrice": 51400, "highest": 51800, "lowest": 50800, "matchVol": 763400,
                              "foreignBuyVolume": 1512506, "foreignSellVolume": 4467288, "time": "2026-11-06T07:45:00.011Z"},
               "bidAsk": {"bidPrices": [{"price": 50800, "volume": 122300}], "askPrices": [{"price": 50900, "volume": 6100}]}}
KBS_ROW = {"SB": "FPT", "CP": 50800, "TT": 12162200, "TV": 622449450000, "RE": 52900, "CL": 56600, "FL": 49200, "OP": 51400,
           "HI": 51800, "LO": 50800, "CV": 763400, "FB": 1512506, "FS": 4467288, "B1": "50800.0", "V1": 122300, "B2": "ATO", "V2": 5,
           "S1": "50900.0", "U1": 6100, "TD": "06/11/2026", "t": 1793953988824}


def test_both_adapters_produce_the_same_quote():
    vietcap, kbs = L.parse_vietcap([VIETCAP_ROW])[0], L.parse_kbs([KBS_ROW])[0]
    for field in ("symbol", "price", "volume", "value", "ref", "ceiling", "floor", "open", "high", "low", "last_volume",
                  "foreign_buy_volume", "foreign_sell_volume", "bids", "asks", "trading_date"):
        assert vietcap[field] == pytest.approx(kbs[field]) if isinstance(vietcap[field], float) else vietcap[field] == kbs[field], field
    assert vietcap["trading_date"] == "2026-11-06"  # 07:45 UTC is still the same VN date
    assert vietcap["source_time"] is not None and kbs["source_time"] is None


def test_adapters_skip_broken_rows():
    assert L.parse_vietcap([{"matchPrice": {"symbol": "FPT"}}, {}]) == []
    assert L.parse_kbs([{"SB": "FPT", "CP": "ATO", "TT": 5}]) == []
    assert L.parse_vietcap({"error": 1}) == [] and L.parse_kbs(None) == []


def test_stock_with_no_trade_keeps_its_reference_data_and_shows_no_price():
    vietcap = L.parse_vietcap([{"listingInfo": {"refPrice": 25000, "ceiling": 28700, "floor": 21300},
                                "matchPrice": {"symbol": "A32", "matchPrice": 0, "accumulatedVolume": 0, "openPrice": 0, "highest": 0, "lowest": 0,
                                               "time": "2026-11-06T08:00:00.042Z"},
                                "bidAsk": {"bidPrices": [{"price": 24500, "volume": 100}], "askPrices": []}}])[0]
    kbs = L.parse_kbs([{"SB": "A32", "CP": 0, "TT": 0, "RE": 25000, "CL": 28700, "FL": 21300, "HI": 0, "LO": 0, "OP": 0, "TD": "06/11/2026", "B1": "24500.0", "V1": 100}])[0]
    for quote_ in (vietcap, kbs):
        assert quote_["price"] is None and quote_["high"] is None and quote_["low"] is None and quote_["open"] is None
        assert quote_["ref"] == 25000 and quote_["volume"] == 0 and quote_["bids"] == [{"price": 24500, "volume": 100}]
    source = Fake("a")
    source.result = [vietcap]
    collector = C.Collector([source], ["A32"], clock=Clock(datetime(2026, 11, 6, 10, 0, tzinfo=core.VN_TZ).timestamp()))
    collector.tick()
    snap = collector.snapshot()
    assert collector.rejected == 0 and snap["missing"] == []
    assert snap["quotes"][0]["change"] is None and snap["quotes"][0]["change_pct"] is None
    json.dumps(snap, allow_nan=False)


def test_a_traded_stock_cannot_lose_its_price_within_the_day():
    collector, a, _ = make()
    a.result = [quote(volume=100.0)]
    collector.tick()
    a.result = [quote(price=None, volume=100.0)]
    collector.tick()
    assert collector.snapshot()["quotes"][0]["price"] == 50800.0
    a.result = [quote(price=None, volume=0.0, date="2026-11-10")]  # a new day may start empty
    collector.tick()
    assert collector.snapshot()["quotes"][0]["price"] is None


# --- replay -----------------------------------------------------------------------------

def replay_bars():
    start = datetime(2026, 11, 6, 9, 15, tzinfo=core.VN_TZ).timestamp()
    return {"FPT": {"ref": 52900.0, "bars": [
        {"t": int(start + 60 * i), "o": 51000 + i, "h": 51100 + i, "l": 50900 - i, "c": 51050 + i, "v": 100} for i in range(3)]}}


def test_replay_advances_and_never_shows_the_future():
    clock = Clock(1000.0)
    source = L.ReplaySource(replay_bars(), "2026-11-06", speed=60, clock=clock)
    first = source.fetch(["FPT"])[0]
    assert first["price"] == 51050 and first["volume"] == 100
    clock.now += 1  # one real second is one session minute
    second = source.fetch(["FPT"])[0]
    assert second["price"] == 51051 and second["volume"] == 200 and second["high"] == 51101 and second["low"] == 50899
    clock.now += 1000
    last = source.fetch(["FPT"])[0]
    assert last["volume"] == 300 and last["bids"] == [] and last["value"] is None and last["source"] == "replay"
    assert source.fetch(["VCB"]) == []


# --- collector --------------------------------------------------------------------------

@pytest.mark.parametrize("moment,state", [
    (datetime(2026, 11, 7, 10, 0), "closed"), (datetime(2026, 11, 9, 8, 0), "closed"), (datetime(2026, 11, 9, 9, 5), "ato"),
    (datetime(2026, 11, 9, 10, 0), "morning"), (datetime(2026, 11, 9, 12, 0), "lunch"), (datetime(2026, 11, 9, 13, 30), "afternoon"),
    (datetime(2026, 11, 9, 14, 40), "atc"), (datetime(2026, 11, 9, 14, 50), "post"), (datetime(2026, 11, 9, 16, 0), "closed"),
])
def test_market_state(moment, state):
    assert C.market_state(moment) == state


def test_poll_interval_follows_the_market():
    collector, _, _ = make()
    assert collector.tick() == C.ACTIVE_POLL_S
    collector, _, _ = make(Clock(SATURDAY))
    assert collector.tick() == C.CLOSED_POLL_S


def test_replay_polls_fast_even_when_the_real_market_is_closed():
    collector, _, _ = make(Clock(SATURDAY), mode="replay")
    assert collector.tick() == C.ACTIVE_POLL_S


def test_persistent_condition_is_logged_once():
    collector, a, b = make()
    a.result = b.result = core.SourceError("HTTP 403")
    for _ in range(5):
        collector.tick()
    settled = len(collector.events)
    for _ in range(20):
        collector.tick()
    assert len(collector.events) == settled


def test_three_failures_switch_to_fallback_and_quotes_keep_flowing():
    clock = Clock()
    collector, a, b = make(clock)
    a.result = core.SourceError("HTTP 500")
    for _ in range(C.FAILURES_BEFORE_SWITCH):
        collector.tick()
    assert collector.active == 1 and b.calls == 0
    collector.tick()
    assert b.calls == 1 and collector.snapshot()["quotes"][0]["source"] == "b"
    assert any("Chuyển từ a sang b" in e["message"] for e in collector.events)


def test_returns_to_primary_when_it_recovers():
    clock = Clock()
    collector, a, b = make(clock)
    a.result = core.SourceError("HTTP 500")
    for _ in range(C.FAILURES_BEFORE_SWITCH):
        collector.tick()
    clock.now += C.PRIMARY_PROBE_S + 1
    collector.tick()  # probe still fails, stays on the fallback
    assert collector.active == 1
    a.result = None
    a.volume = 10_000
    clock.now += C.PRIMARY_PROBE_S + 1
    collector.tick()
    assert collector.active == 0 and any("Quay lại" in e["message"] for e in collector.events)


def test_blocked_source_is_not_called_during_cooldown():
    clock = Clock()
    collector, a, b = make(clock)
    a.result = core.SourceError("https://x trả HTTP 429")
    collector.tick()
    assert collector.active == 1 and a.calls == 1
    a.result = None
    clock.now += C.PRIMARY_PROBE_S + 1
    collector.tick()
    assert a.calls == 1  # no probing while cooling down
    clock.now += C.BLOCK_COOLDOWN_S
    collector.tick()
    assert a.calls == 2 and collector.active == 0


def test_all_sources_blocked_reports_offline_without_calling_them():
    clock = Clock()
    collector, a, b = make(clock)
    a.result = b.result = core.SourceError("HTTP 403")
    collector.tick()
    collector.tick()
    calls = a.calls + b.calls
    clock.now += 60
    collector.tick()
    assert a.calls + b.calls == calls
    assert collector.feed_status()["status"] == "offline"


def test_frozen_source_during_trading_triggers_failover_but_not_at_lunch():
    clock = Clock()
    collector, a, b = make(clock)
    a.result = [quote(volume=100.0, source="a")]
    collector.tick()
    clock.now += C.FROZEN_AFTER_S + 1
    collector.tick()
    assert collector.active == 1 and any("đứng yên" in e["message"] for e in collector.events)

    clock = Clock(datetime(2026, 11, 9, 12, 0, tzinfo=core.VN_TZ).timestamp())
    collector, a, _ = make(clock)
    a.result = [quote(volume=100.0, source="a")]
    collector.tick()
    clock.now += C.FROZEN_AFTER_S + 1
    collector.tick()
    assert collector.active == 0


def test_older_or_impossible_data_is_rejected():
    collector, a, _ = make()
    a.result = [quote(volume=1000.0)]
    collector.tick()
    for bad in (quote(volume=900.0), quote(date="2026-11-06", volume=99999.0), quote(price=99999.0, volume=2000.0), quote(price=0.0, volume=2000.0)):
        a.result = [bad]
        collector.tick()
    held = collector.snapshot()["quotes"][0]
    assert held["volume"] == 1000.0 and held["price"] == 50800.0 and collector.rejected == 4


def test_new_trading_day_may_restart_volume():
    collector, a, _ = make()
    a.result = [quote(volume=1000.0, date="2026-11-06")]
    collector.tick()
    a.result = [quote(volume=5.0, date="2026-11-09")]
    collector.tick()
    assert collector.snapshot()["quotes"][0]["volume"] == 5.0


def test_empty_response_counts_as_failure():
    collector, a, _ = make()
    a.result = []
    for _ in range(C.FAILURES_BEFORE_SWITCH):
        collector.tick()
    assert collector.active == 1


def test_snapshot_reports_age_change_and_missing():
    clock = Clock()
    collector, a, _ = make(clock)
    assert collector.feed_status()["status"] == "starting"
    collector.symbols, collector.known, collector.priority = ["FPT", "VCB"], {"FPT", "VCB"}, ["FPT", "VCB"]
    collector.tick()
    clock.now += 5
    snap = collector.snapshot()
    assert snap["quotes"][0]["age_s"] == 5.0 and snap["quotes"][0]["change_pct"] == 1.6 and snap["missing"] == ["VCB"]
    assert snap["feed"]["status"] == "ok" and snap["feed"]["last_success_age_s"] == 5.0
    clock.now += C.STALE_ACTIVE_S
    assert collector.snapshot()["feed"]["status"] == "offline"
    json.dumps(snap, allow_nan=False)


def test_recorder_writes_only_changes(tmp_path):
    recorder = C.QuoteRecorder(tmp_path / "q.db")
    a = Fake("a")
    a.result = [quote(volume=100.0)]
    collector = C.Collector([a], ["FPT"], clock=Clock(), recorder=recorder)
    collector.tick()
    collector.tick()
    a.result = [quote(volume=200.0)]
    collector.tick()
    assert [r[0] for r in recorder.db.execute("SELECT volume FROM quote_log")] == [100.0, 200.0]
    recorder.close()


def test_session_report_finds_gaps_and_sources(tmp_path):
    recorder = C.QuoteRecorder(tmp_path / "q.db")
    start = datetime(2026, 11, 9, 9, 30, tzinfo=core.VN_TZ).timestamp()
    recorder.write(start, [quote(volume=100.0, source="a")])
    recorder.write(start + 3, [quote(volume=200.0, source="a")])
    recorder.write(start + 303, [quote(volume=300.0, source="b")])  # five quiet minutes while the source changed
    recorder.write(start + 9000, [quote(volume=400.0, source="b")])  # after lunch: the break does not count
    report = recorder.session_report("2026-11-09")
    assert report["updates"] == 4 and report["by_source"] == {"a": 2, "b": 2}
    assert report["largest_gap_s"] == 300.0 and report["largest_gap_at"].startswith("2026-11-09T09:30:03")
    assert report["first"].startswith("2026-11-09T09:30:00")
    assert recorder.session_report("2026-11-10")["updates"] == 0
    recorder.close()


# --- whole market -----------------------------------------------------------------------

class Recording:
    name = "rec"

    def __init__(self):
        self.batches = []
        self.fail = False

    def fetch(self, symbols):
        if self.fail:
            raise core.SourceError("HTTP 500")
        self.batches.append(list(symbols))
        return [quote(symbol=s, source="rec") for s in symbols]


def universe(count=1000):
    return [f"{chr(65 + i // 676)}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}" for i in range(count)]


def test_first_call_loads_everything_then_one_small_call_per_tick():
    source, symbols = Recording(), universe()
    collector = C.Collector([source], symbols, priority=["AAA"], clock=Clock())
    collector.tick()
    assert len(source.batches[0]) == 1000 and len(collector.quotes) == 1000
    collector.tick()
    assert len(source.batches) == 2 and len(source.batches[1]) == 1 + C.ROTATION_SIZE and source.batches[1][0] == "AAA"


def test_failed_first_sweep_is_retried_in_full():
    source, symbols = Recording(), universe(600)
    collector = C.Collector([source], symbols, clock=Clock())
    source.fail = True
    collector.tick()
    source.fail = False
    collector.tick()
    assert len(source.batches[0]) == 600


def test_rotation_reaches_every_symbol_and_watchlist_every_tick():
    source, symbols = Recording(), universe()
    collector = C.Collector([source], symbols, priority=["AAA", "AAB"], clock=Clock())
    collector.tick()
    source.batches.clear()
    needed = len(symbols) // C.ROTATION_SIZE + 1
    for _ in range(needed):
        collector.tick()
    assert set().union(*source.batches) == set(symbols)
    assert all(batch[:2] == ["AAA", "AAB"] for batch in source.batches)


def test_viewed_symbols_join_the_fast_tier_then_leave():
    clock, source, symbols = Clock(), Recording(), universe()
    collector = C.Collector([source], symbols, priority=["AAA"], clock=clock)
    collector.tick()
    wanted = symbols[500:503]
    collector.snapshot(wanted)
    collector.tick()
    assert source.batches[-1][0] == "AAA" and set(source.batches[-1][1:4]) == set(wanted)
    clock.now += C.VIEW_TTL_S + 1
    collector.tick()
    assert source.batches[-1][0] == "AAA" and collector.viewed == {}  # only the watchlist remains fast


def test_fast_tier_is_capped_and_unknown_symbols_are_not_tracked():
    clock, source, symbols = Clock(), Recording(), universe()
    collector = C.Collector([source], symbols, priority=["AAA"], clock=clock)
    collector.tick()
    collector.snapshot(symbols[:400] + ["ZZZ"])
    assert "ZZZ" not in collector.viewed
    collector.tick()
    assert len(source.batches[-1]) == C.MAX_PRIORITY + C.ROTATION_SIZE


def test_only_the_watchlist_is_recorded(tmp_path):
    recorder = C.QuoteRecorder(tmp_path / "q.db")
    collector = C.Collector([Recording()], universe(300), priority=["AAA"], clock=Clock(), recorder=recorder)
    collector.tick()
    assert [r[0] for r in recorder.db.execute("SELECT DISTINCT symbol FROM quote_log")] == ["AAA"]
    recorder.close()


def test_universe_keeps_only_active_listed_stocks():
    rows = [
        {"symbol": "FPT", "type": "STOCK", "board": "HSX", "organShortName": "FPT Corp"},
        {"symbol": "ACB", "type": "STOCK", "board": "HNX", "organName": "Á Châu"},
        {"symbol": "AAM", "type": "STOCK", "board": "UPCOM"},
        {"symbol": "OLD", "type": "STOCK", "board": "DELISTED"},
        {"symbol": "CFPT2501", "type": "COVERED_WARRANT", "board": "HSX"},
        {"symbol": "E1VFVN30", "type": "STOCK", "board": "HSX"},
    ]
    assert L.parse_universe(rows) == [
        {"symbol": "AAM", "exchange": "UPCOM", "name": ""},
        {"symbol": "ACB", "exchange": "HNX", "name": "Á Châu"},
        {"symbol": "FPT", "exchange": "HOSE", "name": "FPT Corp"},
    ]
    assert L.parse_universe({"bad": 1}) == []


def test_universe_uses_cache_then_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "load_watchlist", lambda: {"symbols": {"FPT": {"name": "F"}}})
    rows = [{"symbol": f"A{i:02d}", "type": "STOCK", "board": "HSX"} for i in range(99)] * 1
    many = [{"symbol": f"{chr(65 + i // 26)}{chr(65 + i % 26)}X", "type": "STOCK", "board": "HSX"} for i in range(600)]
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: rows)  # too few rows: a broken response is not kept
    assert L.load_universe() == [{"symbol": "FPT", "exchange": None, "name": "F"}]
    assert not (tmp_path / "live" / "universe.json").exists()
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: many)
    assert len(L.load_universe()) == 600
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: pytest.fail("fresh cache should answer"))
    assert len(L.load_universe()) == 600


def test_symbols_endpoint_lists_the_market_and_needs_an_invite(tmp_path):
    source = Recording()
    collector = C.Collector([source], ["FPT", "VCB"], priority=["FPT"], clock=Clock())
    store = InviteStore(tmp_path / "i.json")
    token = store.add("An")
    entries = [{"symbol": "FPT", "exchange": "HOSE", "name": "FPT"}]
    with TestClient(create_app(collector, store, [], universe=entries)) as test_client:
        assert test_client.get("/api/symbols").status_code == 401
        body = test_client.get("/api/symbols", headers=auth(token)).json()
        assert body == {"symbols": entries, "watchlist": ["FPT"]}


# --- API --------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    collector, _, _ = make()
    collector.tick()
    store = InviteStore(tmp_path / "invites.json")
    token = store.add("An")
    app = create_app(collector, store, ["https://example.github.io"])
    with TestClient(app) as test_client:
        test_client.token, test_client.store = token, store
        yield test_client


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_quotes_need_a_valid_invite(client):
    assert client.get("/api/quotes").status_code == 401
    assert client.get("/api/quotes", headers=auth("wrong")).status_code == 401
    assert client.get("/api/status").status_code == 401
    response = client.get("/api/quotes", headers=auth(client.token))
    assert response.status_code == 200 and response.json()["quotes"][0]["symbol"] == "FPT"
    assert response.headers["cache-control"] == "no-store"


def test_token_in_query_string_is_not_accepted(client):
    assert client.get(f"/api/quotes?token={client.token}").status_code == 401


def test_revoke_applies_at_once_and_only_the_hash_is_stored(client, tmp_path):
    assert client.token not in (tmp_path / "invites.json").read_text(encoding="utf-8")
    client.store.revoke("An")
    assert client.get("/api/quotes", headers=auth(client.token)).status_code == 401
    with pytest.raises(ValueError):
        client.store.revoke("Không có")


def test_duplicate_active_invite_refused_and_reissue_after_revoke(tmp_path):
    store = InviteStore(tmp_path / "i.json")
    old = store.add("An")
    with pytest.raises(ValueError):
        store.add("An")
    store.revoke("An")
    new = store.add("An")
    assert store.who(new) == "An" and store.who(old) is None
    with pytest.raises(ValueError):
        store.add("  ")


def test_only_read_only_routes_exist(client):
    headers = auth(client.token)
    assert client.post("/api/quotes", headers=headers).status_code == 405
    for path in ("/docs", "/openapi.json", "/redoc", "/", "/../data/live/invites.json"):
        assert client.get(path, headers=headers).status_code == 404
    assert client.get("/api/health").json() == {"status": "ok"}


def test_bad_symbols_are_rejected(client):
    response = client.get("/api/quotes", params={"symbols": "FPT,../x"}, headers=auth(client.token))
    assert response.status_code == 400
    assert client.get("/api/quotes", params={"symbols": "FPT"}, headers=auth(client.token)).json()["missing"] == []


def test_cors_allows_only_the_configured_origin(client):
    allowed = client.get("/api/health", headers={"Origin": "https://example.github.io"})
    assert allowed.headers["access-control-allow-origin"] == "https://example.github.io"
    assert "access-control-allow-origin" not in client.get("/api/health", headers={"Origin": "https://evil.test"}).headers


def test_rate_limit_is_per_viewer(tmp_path):
    collector, _, _ = make()
    store = InviteStore(tmp_path / "i.json")
    one, two = store.add("An"), store.add("Bình")
    clock = Clock(0.0)
    with TestClient(create_app(collector, store, [], limiter=RateLimiter(2, clock))) as test_client:
        assert test_client.get("/api/quotes", headers=auth(one)).status_code == 200
        assert test_client.get("/api/quotes", headers=auth(one)).status_code == 200
        assert test_client.get("/api/quotes", headers=auth(one)).status_code == 429
        assert test_client.get("/api/quotes", headers=auth(two)).status_code == 200
        clock.now = 61.0
        assert test_client.get("/api/quotes", headers=auth(one)).status_code == 200


# --- candles ----------------------------------------------------------------------------

def test_bars_cache_limits_calls_and_falls_back_to_cached_on_error(monkeypatch):
    clock, calls = Clock(1000.0), []

    def fake(url, **kwargs):
        calls.append(url)
        return [{"t": ["1793950080"], "o": [1], "h": [2], "l": [1], "c": [2], "v": [5]}]

    monkeypatch.setattr(core, "fetch_json", fake)
    provider = BarProvider(["FPT"], clock=clock)
    assert provider.get("FPT", "1m")["bars"][0]["close"] == 2
    provider.get("FPT", "1m")
    assert len(calls) == 1
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: (_ for _ in ()).throw(core.SourceError("down")))
    clock.now += 100
    stale = provider.get("FPT", "1m")
    assert stale["bars"] and "đã lưu" in stale["warnings"][-1]
    with pytest.raises(KeyError):
        provider.get("VCB", "1m")
    with pytest.raises(ValueError):
        provider.get("FPT", "5m")


def test_replay_bars_never_show_the_future():
    clock = Clock(1000.0)
    source = L.ReplaySource(replay_bars(), "2026-11-06", speed=60, clock=clock)
    provider = BarProvider(["FPT"], replay=source, clock=clock)
    assert len(provider.get("FPT", "1m")["bars"]) == 1
    clock.now += 1
    assert len(provider.get("FPT", "1m")["bars"]) == 2


def test_bars_endpoint_rejects_symbols_outside_the_watchlist(tmp_path, monkeypatch):
    collector, _, _ = make()
    store = InviteStore(tmp_path / "i.json")
    token = store.add("An")
    provider = BarProvider(["FPT"])
    monkeypatch.setattr(provider, "_fetch_live", lambda symbol, interval: [{"time": 1}])
    with TestClient(create_app(collector, store, [], bars=provider)) as test_client:
        params = {"symbol": "FPT", "interval": "1m"}
        assert test_client.get("/api/bars", params=params).status_code == 401
        assert test_client.get("/api/bars", params=params, headers=auth(token)).status_code == 200
        assert test_client.get("/api/bars", params={"symbol": "VCB"}, headers=auth(token)).status_code == 404
        assert test_client.get("/api/bars", params={"symbol": "../x"}, headers=auth(token)).status_code == 400
        assert test_client.get("/api/bars", params={"symbol": "FPT", "interval": "9h"}, headers=auth(token)).status_code == 400


# --- real sources -----------------------------------------------------------------------

@pytest.mark.live
def test_live_sources_agree():
    symbols = ["FPT", "VCB", "HPG"]
    vietcap = {q["symbol"]: q for q in L.VietcapSource().fetch(symbols)}
    kbs = {q["symbol"]: q for q in L.KbsSource().fetch(symbols)}
    assert set(vietcap) == set(kbs) == set(symbols)
    for symbol in symbols:
        for field in ("ref", "ceiling", "floor"):
            assert vietcap[symbol][field] == kbs[symbol][field], (symbol, field)
        assert vietcap[symbol]["price"] > 0 and vietcap[symbol]["bids"] and vietcap[symbol]["asks"]
        json.dumps([vietcap[symbol], kbs[symbol]], allow_nan=False)


@pytest.mark.live
def test_live_universe_covers_all_three_exchanges():
    entries = L.load_universe()
    counts = {exchange: sum(e["exchange"] == exchange for e in entries) for exchange in ("HOSE", "HNX", "UPCOM")}
    assert all(count > 100 for count in counts.values())
    assert len(L.VietcapSource().fetch([e["symbol"] for e in entries])) == len(entries)


# --- holiday calendar -------------------------------------------------------------------

def test_listed_holiday_is_quiet_and_never_frozen():
    holiday = datetime(2026, 9, 2, 10, 0, tzinfo=core.VN_TZ)
    assert C.market_state(holiday) == "holiday"  # from config/holidays.json
    assert C.market_state(holiday, holidays=frozenset()) == "morning"
    clock = Clock(holiday.timestamp())
    collector, a, _ = make(clock)
    a.result = [quote(volume=100.0)]
    assert collector.tick() == C.CLOSED_POLL_S
    clock.now += C.FROZEN_AFTER_S * 3
    collector.tick()
    assert collector.active == 0 and not any("đứng yên" in e["message"] for e in collector.events)
    assert collector.feed_status()["market"] == "holiday"


def test_holiday_file_loads_observed_and_announced(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"covers_until": "2027-01-01", "observed": {"dates": ["2026-09-02"]},
                                "announced": [{"date": "2027-01-01"}]}), encoding="utf-8")
    assert C.load_holidays(path) == (frozenset({"2026-09-02", "2027-01-01"}), "2027-01-01")
    assert C.load_holidays(tmp_path / "missing.json") == (frozenset(), None)
    path.write_text("{broken", encoding="utf-8")
    assert C.load_holidays(path) == (frozenset(), None)


def test_shipped_holiday_calendar_is_valid():
    dates, until = C.load_holidays()
    assert until and len(dates) >= 70
    parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    assert all(d.weekday() < 5 for d in parsed)  # weekends are closed anyway; a weekend entry is a typo
    assert max(d.strftime("%Y-%m-%d") for d in parsed) <= until


@pytest.mark.parametrize("today,until,expected", [
    (datetime(2026, 11, 11).date(), "2027-01-01", None),
    (datetime(2026, 11, 20).date(), "2027-01-01", "2027-01-01"),
    (datetime(2027, 2, 1).date(), "2027-01-01", "2027-01-01"),
    (datetime(2026, 11, 11).date(), None, "holidays.json"),
])
def test_calendar_warning_before_it_runs_out(today, until, expected):
    warning = C.holiday_calendar_warning(today, until)
    assert warning is None if expected is None else expected in warning
