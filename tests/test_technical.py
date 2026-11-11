"""Technical module tests. Expected values are written out or brute-forced here, never taken from the module."""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stock_analyst import core, technical as tech

# The StockCharts RSI worked-example closes. The first five values are its published RSI(14), as recalled;
# later published values were computed from rounded changes, so the full series is checked by brute force.
WILDER_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
                 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6400,
                 46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
                 43.4205, 42.6628, 43.1314]
WILDER_RSI = [70.53, 66.32, 66.55, 69.41, 66.36]

pytestmark = pytest.mark.filterwarnings("ignore:The 'generic' unit:DeprecationWarning")  # pandas 2.3 under numpy 2.5


def frame(close, high=None, low=None, open_=None, volume=None, start="2024-01-01") -> pd.DataFrame:
    close = np.asarray(close, float)
    return pd.DataFrame({
        "open": close if open_ is None else open_, "high": close if high is None else high,
        "low": close if low is None else low, "close": close,
        "volume": np.full(len(close), 1000.0) if volume is None else np.asarray(volume, float),
    }, index=pd.bdate_range(start, periods=len(close), name="date"))


def wave(n: int, seed: int = 7) -> pd.DataFrame:
    """Reproducible random walk with cycles, so that every signal family has a chance to fire."""
    rng = np.random.default_rng(seed)
    close = 50000 * np.exp(np.cumsum(rng.normal(0, 0.016, n)) + 0.22 * np.sin(np.arange(n) / 22))
    spread = close * (0.008 + 0.012 * rng.random(n))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(500_000, 3_000_000, n).astype(float)
    volume[rng.integers(0, n, max(n // 25, 1))] *= 5
    return frame(close, high, low, open_, volume)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(core, "now_vn", lambda: datetime(2026, 11, 7, 10, 0, tzinfo=core.VN_TZ))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline test tried to reach the network")
    monkeypatch.setattr(core, "fetch", refuse)
    monkeypatch.setattr(core, "fetch_json", refuse)


# ------------------------------------------------------------ indicator formulas
def test_sma_hand_values():
    result = tech.sma(pd.Series([1.0, 2, 3, 4, 5]), 3)
    assert result.isna().tolist() == [True, True, False, False, False]
    assert result.tolist()[2:] == [2.0, 3.0, 4.0]


def test_ema_is_sma_seeded_hand_values():
    # n=3, alpha=0.5: seed mean(2,4,6)=4; then 4+0.5*(8-4)=6; then 6+0.5*(10-6)=8
    result = tech.ema(pd.Series([2.0, 4, 6, 8, 10]), 3)
    assert result.isna().sum() == 2
    assert result.tolist()[2:] == [4.0, 6.0, 8.0]


def test_rsi_matches_published_wilder_example():
    result = tech.rsi(pd.Series(WILDER_CLOSES))
    assert result.iloc[:14].isna().all()
    assert result.iloc[14:19].round(2).tolist() == WILDER_RSI
    changes = [b - a for a, b in zip(WILDER_CLOSES, WILDER_CLOSES[1:])]
    gain = sum(max(c, 0) for c in changes[:14]) / 14
    loss = sum(max(-c, 0) for c in changes[:14]) / 14
    expected = [100 - 100 / (1 + gain / loss)]
    for change in changes[14:]:
        gain, loss = (gain * 13 + max(change, 0)) / 14, (loss * 13 + max(-change, 0)) / 14
        expected.append(100 - 100 / (1 + gain / loss))
    assert result.iloc[14:].tolist() == pytest.approx(expected)


def test_rsi_brute_force_small_window():
    # n=3, changes +2,-1,+3,-2. seed gain=5/3, loss=1/3 -> RSI 83.3333
    # next gain=(5/3*2+0)/3=10/9, loss=(1/3*2+2)/3=8/9 -> RSI=100*10/18=55.5556
    result = tech.rsi(pd.Series([10.0, 12, 11, 14, 12]), 3)
    assert result.iloc[3] == pytest.approx(83.3333, abs=1e-3)
    assert result.iloc[4] == pytest.approx(55.5556, abs=1e-3)


def test_rsi_all_gains_is_100_and_all_losses_is_0():
    assert tech.rsi(pd.Series(np.arange(1.0, 30))).iloc[-1] == 100
    assert tech.rsi(pd.Series(np.arange(30.0, 1, -1))).iloc[-1] == 0


def brute_macd(closes, fast=12, slow=26, signal=9):
    def brute_ema(values, n):
        out = [None] * (n - 1) + [sum(values[:n]) / n]
        for v in values[n:]:
            out.append(out[-1] + (v - out[-1]) * 2 / (n + 1))
        return out
    f, s = brute_ema(closes, fast), brute_ema(closes, slow)
    line = [a - b for a, b in zip(f[slow - 1:], s[slow - 1:])]
    return line[-1], brute_ema(line, signal)[-1]


def test_macd_against_pure_python_recursion():
    closes = [100 + 7 * math.sin(i / 4) + i * 0.1 for i in range(60)]
    line, signal = brute_macd(closes)
    result = tech.macd(pd.Series(closes))
    assert result["macd"].iloc[-1] == pytest.approx(line)
    assert result["macd_signal"].iloc[-1] == pytest.approx(signal)
    assert result["macd_hist"].iloc[-1] == pytest.approx(line - signal)
    assert result["macd"].first_valid_index() == 25 and result["macd_signal"].first_valid_index() == 33


def test_stochastic_hand_values():
    # n=3,k=2,d=2. raw%K: bar2 (5-1)/(6-1)=80; bar3 (7-2)/(8-2)=83.333; bar4 (6-3)/(9-3)=50
    df = frame([3, 4, 5, 7, 6], high=[4, 5, 6, 8, 9], low=[1, 2, 3, 4, 5])
    result = tech.stochastic(df, 3, 2, 2)
    assert result["stoch_k"].iloc[3] == pytest.approx((80 + 500 / 6) / 2)
    assert result["stoch_k"].iloc[4] == pytest.approx((500 / 6 + 50) / 2)
    assert result["stoch_d"].iloc[4] == pytest.approx(((80 + 500 / 6) / 2 + (500 / 6 + 50) / 2) / 2)


def test_bollinger_population_std_hand_values():
    # 2,4,4,4,5,5,7,9: mean 5, population std exactly 2
    result = tech.bollinger(pd.Series([2.0, 4, 4, 4, 5, 5, 7, 9]), 8, 2)
    assert result["bb_middle"].iloc[-1] == 5
    assert result["bb_upper"].iloc[-1] == 9 and result["bb_lower"].iloc[-1] == 1
    assert result["bb_percent_b"].iloc[-1] == 1  # close sits on the upper band
    assert result["bb_bandwidth"].iloc[-1] == pytest.approx(8 / 5)


def test_atr_wilder_hand_values():
    # TR: 2 (H-L), then max(3-1, |3-2|, |1-2|)=2, then max(5-4,|5-2|,|4-2|)=3, then max(6-3,|6-4|,|3-4|)=3
    df = frame([2, 2, 4, 4], high=[3, 3, 5, 6], low=[1, 1, 4, 3])
    assert tech.true_range(df).tolist() == [2, 2, 3, 3]
    result = tech.atr(df, 3)  # seed (2+2+3)/3, then (7/3*2+3)/3
    assert result.iloc[2] == pytest.approx(7 / 3)
    assert result.iloc[3] == pytest.approx((7 / 3 * 2 + 3) / 3)


def brute_adx(high, low, close, n):
    plus, minus, tr = [], [], []
    for i in range(1, len(close)):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    # Wilder's running-sum form: S = S - S/n + x
    s_tr, s_p, s_m = sum(tr[:n]), sum(plus[:n]), sum(minus[:n])
    pdi, mdi = [100 * s_p / s_tr], [100 * s_m / s_tr]
    for i in range(n, len(tr)):
        s_tr += tr[i] - s_tr / n
        s_p += plus[i] - s_p / n
        s_m += minus[i] - s_m / n
        pdi.append(100 * s_p / s_tr)
        mdi.append(100 * s_m / s_tr)
    dx = [100 * abs(p - m) / (p + m) for p, m in zip(pdi, mdi)]
    adx_value = sum(dx[:n]) / n
    for value in dx[n:]:
        adx_value = (adx_value * (n - 1) + value) / n
    return pdi[-1], mdi[-1], adx_value


def test_adx_against_wilder_running_sum_form():
    df = wave(80)
    expected = brute_adx(df["high"].tolist(), df["low"].tolist(), df["close"].tolist(), 14)
    result = tech.adx(df).iloc[-1]
    assert (result["plus_di14"], result["minus_di14"], result["adx14"]) == pytest.approx(expected)
    assert tech.adx(df)["adx14"].first_valid_index() == df.index[27]


def test_obv_hand_values():
    df = frame([10, 11, 11, 9, 10], volume=[100, 200, 300, 400, 500])
    assert tech.obv(df).tolist() == [0, 200, 200, -200, 300]


def test_mfi_hand_values():
    # Typical price = close here. n=2. Flows 20*2=40 (up), 15*3=45 (down), 15*1=15 (equal, ignored), 30*4=120 (up)
    df = frame([10, 20, 15, 15, 30], volume=[1, 2, 3, 1, 4])
    result = tech.mfi(df, 2)
    assert result.iloc[2] == pytest.approx(100 * 40 / 85)
    assert result.iloc[3] == pytest.approx(0.0)  # +0 over -45-0
    assert result.iloc[4] == pytest.approx(100.0)


def test_52_week_window_is_calendar_based_and_null_until_full():
    df = frame(np.arange(100.0, 400.0))
    result = tech.rolling_52w(df)
    assert result["high_52w"].iloc[100:].notna().sum() < 200 and pd.isna(result["high_52w"].iloc[100])
    assert result["high_52w"].iloc[-1] == 399
    cutoff = df.index[-1] - pd.Timedelta(days=365)  # the rolling window is open on the left
    assert result["low_52w"].iloc[-1] == df["low"].loc[df.index > cutoff].min()


# ------------------------------------------------------------ data handling
def test_vietcap_timestamps_land_on_vn_date():
    # 2026-11-06: 00:00 UTC is how Vietcap stamps it; DNSE uses 02:00 UTC; midnight VN is 17:00 UTC the day before.
    dates = tech._vn_dates([1793923200, 1793930400, 1793923200 - 7 * 3600, "1793923200"])
    assert [d.date().isoformat() for d in dates] == ["2026-11-06"] * 4


def test_all_source_shapes_align_on_one_trading_date():
    vietcap = tech._parse_vietcap([{"t": ["1793923200"], "o": [51400], "h": [51800], "l": [50800], "c": [50800],
                                    "v": [12162200], "accumulatedValue": [622449.45]}])
    kbs = tech._parse_kbs({"data_day": [{"t": "2026-11-06 07:00", "o": "51400", "h": 51800, "l": 50800, "c": 50800, "v": 12162200}]})
    assert vietcap.index[0] == kbs.index[0] == pd.Timestamp("2026-11-06")
    assert vietcap.index[0].weekday() == 4
    assert vietcap["value"].iloc[0] == pytest.approx(622449450000)
    assert kbs["open"].iloc[0] == 51400 and math.isnan(kbs["value"].iloc[0])


def test_clean_sorts_dedupes_drops_junk_and_invents_nothing():
    raw = pd.DataFrame({
        "open": [12, 10, 10, 11, 0, 13], "high": [12, 11, 11, 12, 0, 14], "low": [12, 9, 9, 10, 0, 12],
        "close": [12, 10, 10.5, 11, 0, 13], "volume": [0, 5, 6, 7, 0, 0], "value": [np.nan] * 6,
    }, index=pd.to_datetime(["2026-11-05", "2026-11-02", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-06"]))
    cleaned, warnings = tech._clean(raw)
    assert [d.day for d in cleaned.index] == [2, 3, 6]        # the gap on 4th and 5th stays a gap
    assert cleaned["close"].iloc[0] == 10.5                   # the later duplicate wins
    assert any("không có giao dịch" in w for w in warnings)   # flat zero-volume filler dropped
    assert any("giá không hợp lệ" in w for w in warnings)
    assert any("khối lượng 0" in w for w in warnings)         # the ranging bar with no volume is kept and flagged


def test_clean_flags_weekend_bars():
    raw = frame([10.0, 11.0], start="2026-11-06")
    raw.index = pd.to_datetime(["2026-11-06", "2026-11-07"])
    assert any("cuối tuần" in w for w in tech._clean(raw)[1])


def test_load_falls_back_to_kbs(monkeypatch):
    def broken(*args, **kwargs):
        raise core.SourceError("down")
    monkeypatch.setattr(tech, "_vietcap", broken)
    rows = {"data_day": [{"t": f"2026-11-0{d} 07:00", "o": 10, "h": 11, "l": 9, "c": 10, "v": 5} for d in (6, 5, 4)]}
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: rows)
    result = tech.get_prices("fpt", 2)
    assert result.attrs["source"] == "kbs" and len(result) == 2
    assert list(result.columns) == tech.COLUMNS and result.index.is_monotonic_increasing
    assert any("KBS" in w for w in result.attrs["warnings"])


def test_load_refetches_when_filler_rows_consume_count_back(monkeypatch):
    calls = []

    def fake(symbol, days):
        calls.append(days)
        df = frame([10.0] * days, volume=[0.0, 5.0] * (days // 2))
        df["value"] = np.nan
        return df
    monkeypatch.setattr(tech, "_vietcap", fake)
    assert len(tech.get_prices("PMP", 4)) == 4
    assert calls == [4, 20]


def test_load_raises_source_error_when_every_source_is_empty(monkeypatch):
    monkeypatch.setattr(core, "fetch_json", lambda url, **k: [] if "vietcap" in url else {"data_day": []})
    with pytest.raises(core.SourceError):
        tech.get_prices("ZZZ")


def test_get_prices_rejects_bad_symbol(no_network):
    with pytest.raises(ValueError):
        tech.get_prices("../etc")
    with pytest.raises(ValueError):
        tech.analyze("VNINDEX")


@pytest.mark.parametrize("now, last, expected", [
    (datetime(2026, 11, 7, 10, 0), "2026-11-06", None),            # Saturday, Friday's bar is current
    (datetime(2026, 11, 8, 10, 0), "2026-11-06", None),            # Sunday
    (datetime(2026, 11, 9, 10, 0), "2026-11-06", None),            # Monday session has not closed
    (datetime(2026, 11, 9, 16, 0), "2026-11-06", "chậm 1"),        # Monday evening, soft
    (datetime(2026, 11, 9, 11, 0), "2026-11-09", "đang diễn ra"),  # in-progress candle
    (datetime(2026, 11, 7, 10, 0), "2026-10-29", "chậm 6"),        # beyond the holiday allowance
    (datetime(2026, 11, 7, 10, 0), "2026-11-09", "sau ngày"),      # future bar
])
def test_staleness_calendar(now, last, expected):
    warnings = tech._staleness(pd.Timestamp(last), now)
    assert (not warnings) if expected is None else (expected in warnings[0])


def test_gap_warning_catches_unadjusted_drop_only_above_exchange_band():
    close = [100.0, 100, 80, 80]  # -20%: looks like an unadjusted 25% stock dividend
    assert "2024-01-03" in tech._gap_warnings(frame(close), "HOSE")[0]
    assert tech._gap_warnings(frame([100.0, 94, 88]), "HOSE") == []    # within the band
    assert tech._gap_warnings(frame(close), "UPCOM") == []             # UPCoM moves wider
    separated = frame(close)
    separated.index = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-03-01", "2024-03-04"])
    assert tech._gap_warnings(separated, "HOSE") == []                 # bars weeks apart are not one session


def vnd_payload(dates, closes, adjusted, floor="HOSE"):
    return {"data": [{"date": str(d.date()), "close": c / 1000, "adClose": a / 1000, "floor": floor}
                     for d, c, a in zip(dates, closes, adjusted)]}


def test_crosscheck_recognises_adjusted_series_and_ex_dates(monkeypatch):
    df = frame([50000.0, 50000, 45000, 45000])  # adjusted series
    raw = [100000.0, 100000, 45000, 45000]      # 2:1-like ex-date on bar 2
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: vnd_payload(df.index, raw, df["close"]))
    basis, warnings, floor = tech._crosscheck("FPT", df)
    assert basis["adjusted"] is True and basis["ex_dates"] == ["2024-01-03"] and floor == "HOSE"
    assert any("khối lượng không được điều chỉnh" in w for w in warnings)


def test_crosscheck_flags_unadjusted_series(monkeypatch):
    raw = [100000.0, 100000, 45000, 45000]
    df = frame(raw)
    monkeypatch.setattr(core, "fetch_json", lambda *a, **k: vnd_payload(df.index, raw, [50000.0, 50000, 45000, 45000]))
    basis, warnings, _ = tech._crosscheck("FPT", df)
    assert basis["adjusted"] is False and any("CHƯA điều chỉnh" in w for w in warnings)


def test_crosscheck_survives_vndirect_failure(monkeypatch):
    def broken(*a, **k):
        raise core.SourceError("down")
    monkeypatch.setattr(core, "fetch_json", broken)
    basis, warnings, floor = tech._crosscheck("FPT", frame([1.0, 2.0]))
    assert basis["adjusted"] is None and floor is None and "Không đối chiếu" in warnings[0]


# ------------------------------------------------------------ analyze on synthetic data
def assert_pure_json(result):
    text = json.dumps(result, allow_nan=False)
    assert json.loads(text) == result


def build(df, index_close=None, attrs=None):
    df = df.copy()
    df.attrs.update(attrs or {"source": "test", "warnings": []})
    return tech._build("AAA", df, index_close, chart=False)


def test_flat_series_gives_nulls_not_division_errors(no_network):
    df = frame([50000.0] * 300, start="2025-09-01")
    result = build(df, df["close"])
    assert_pure_json(result)
    values = result["indicators"]
    for name in ("rsi14", "adx14", "plus_di14", "minus_di14", "stoch_k", "stoch_d", "bb_percent_b", "mfi14"):
        assert values[name] is None, name
    assert values["sma200"] == 50000 and values["atr14"] == 0 and values["bb_bandwidth"] == 0
    assert result["volatility"]["realised_20_annualised_pct"] == 0 and result["volatility"]["max_drawdown_pct"] == 0
    assert result["trend"]["long"]["state"] == "mixed"
    assert result["signal_history"]["types"] == {} and result["signals"] == []
    assert result["levels"]["support"] == [] and result["levels"]["resistance"] == []
    assert any("mẫu số bằng 0" in w for w in result["warnings"])


@pytest.mark.parametrize("n", [1, 2, 10, 30])
def test_short_history_returns_nulls_with_warnings(no_network, n):
    result = build(wave(n), None)
    assert_pure_json(result)
    values = result["indicators"]
    assert values["sma200"] is None and values["high_52w"] is None and result["relative_strength"] is None
    assert result["trend"]["long"]["state"] is None
    assert result["volatility"]["realised_20_annualised_pct"] is None or n > 20
    assert any("Thiếu lịch sử" in w and "sma200 (cần 200)" in w for w in result["warnings"])
    assert any("52 tuần" in w for w in result["warnings"])
    assert any("VNINDEX" in w for w in result["warnings"])


def test_full_result_shape_and_rules(no_network):
    df = wave(500)
    df.index = pd.bdate_range(end="2026-11-06", periods=500, name="date")
    result = build(df, df["close"] * 1.0)
    assert_pure_json(result)
    assert result["as_of"] == "2026-11-06"
    assert result["change"]["1D"]["pct"] == pytest.approx((df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100, abs=0.01)
    ytd_base = df["close"].loc[:"2025-12-31"].iloc[-1]
    assert result["change"]["YTD"]["pct"] == pytest.approx((df["close"].iloc[-1] / ytd_base - 1) * 100, abs=0.01)
    assert result["relative_strength"]["3M"]["excess_pct"] == 0
    for horizon in ("short", "medium", "long"):
        assert result["trend"][horizon]["state"] in {"up", "down", "mixed"} and result["trend"][horizon]["rule"]
    close = df["close"].iloc[-1]
    assert all(level["price"] < close for level in result["levels"]["support"])
    assert all(level["price"] >= close for level in result["levels"]["resistance"])
    assert any("không dự báo tương lai" in w for w in result["warnings"])
    text = json.dumps(result, ensure_ascii=False).lower()
    assert not any(word in text for word in ("khuyến nghị", "nên mua", "nên bán", "giá mục tiêu", "score", '"buy"', '"sell"'))
    expected = {e["type"] for e in tech.detect_signals(df[tech.COLUMNS], tech.compute_indicators(df[tech.COLUMNS]))}
    assert set(result["signal_history"]["types"]) == expected and len(expected) >= 8


def test_volatility_and_drawdown_hand_values():
    # Peak 200 then trough 50: -75%. Alternating log returns of +/-r over 20 sessions.
    result = tech._volatility(pd.Series([100.0, 200, 50, 100], index=pd.bdate_range("2026-01-01", periods=4)))
    assert result["max_drawdown_pct"] == -75 and result["max_drawdown_peak_date"] == "2026-01-02"
    r = 0.01
    closes = 100 * np.exp(np.cumsum([r if i % 2 else -r for i in range(21)]))
    returns = [r if i % 2 else -r for i in range(1, 21)]
    mean = sum(returns) / 20
    sample_std = math.sqrt(sum((x - mean) ** 2 for x in returns) / 19)
    series = pd.Series(closes, index=pd.bdate_range("2026-01-01", periods=21))
    assert tech._volatility(series)["realised_20_annualised_pct"] == pytest.approx(sample_std * math.sqrt(252) * 100, abs=0.01)


# ------------------------------------------------------------ levels and signals
def zigzag() -> pd.DataFrame:
    """Rises to about 120 twice and falls to about 100 three times over 7-bar legs."""
    leg = [100, 103, 106, 110, 114, 117, 120, 117, 114, 110, 106, 103]
    close = np.array(leg * 3 + [100, 103, 106, 110], float)
    return frame(close, high=close + 0.5, low=close - 0.5, open_=close)


def test_levels_cluster_repeated_swings_with_touches_and_last_date():
    df = zigzag()
    levels = tech.find_levels(df, tech.compute_indicators(df))
    resistance = levels["resistance"][0]
    assert resistance["price"] == 120.5 and resistance["touches"] == 3
    assert resistance["last_touch"] == tech._date(df.index[30])
    support = levels["support"][0]
    assert support["price"] == 99.5 and support["touches"] == 2       # bars 12 and 24; bar 0 and 36 lack a full window
    assert resistance["distance_pct"] == pytest.approx((120.5 / 110 - 1) * 100, abs=0.01)
    atr_now = tech.atr(df).iloc[-1]
    assert resistance["distance_atr"] == pytest.approx(10.5 / atr_now, abs=0.01)


def test_pivots_ignore_unconfirmed_bars():
    close = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], float)  # still rising at the end
    assert tech._pivots(frame(close)) == ([], [])


def events_of(df):
    return tech.detect_signals(df, tech.compute_indicators(df))


def test_crossovers_fire_on_exact_bar():
    # 60 flat bars at 100 then a rise: SMA20 goes above SMA50 on the first bar of the rise.
    close = np.r_[np.full(60, 100.0), 100 + np.arange(1, 15.0)]
    df = frame(close)
    crosses = [e for e in events_of(df) if e["type"] == "sma20_cross_above_sma50"]
    assert [e["bar"] for e in crosses] == [60]


def test_rsi_extremes_and_volume_spike_fire_once_on_entry():
    close = np.r_[100 + np.sin(np.arange(40.0)), 100 + np.arange(1, 20.0) * 1.0]
    volume = np.full(len(close), 1000.0)
    volume[30] = 5000
    events = events_of(frame(close, volume=volume))
    spikes = [e for e in events if e["type"] == "volume_spike"]
    assert [e["bar"] for e in spikes] == [30] and spikes[0]["evidence"]["volume"] == 5000
    # Hand ratio: 5000 / ((19*1000+5000)/20) = 5000/1200
    assert spikes[0]["evidence"]["volume_ratio20"] == pytest.approx(4.17, abs=0.01)
    assert len([e for e in events if e["type"] == "rsi_overbought"]) >= 1
    assert not [e for e in events if e["type"] == "rsi_oversold" and e["bar"] > 40]


def test_new_52_week_high_uses_prior_window_only():
    close = np.r_[np.full(300, 100.0), [105.0, 105.0, 110.0]]
    events = [e for e in events_of(frame(close)) if e["type"] == "new_52w_high"]
    assert [e["bar"] for e in events] == [300, 302]  # bar 301 only equals the high


def test_breakout_needs_earlier_confirmed_resistance():
    df = zigzag()
    rising = np.array([114, 117, 119, 122, 124], float)
    close = np.r_[df["close"].to_numpy(), rising]
    breakout_df = frame(close, high=close + 0.5, low=close - 0.5)
    events = [e for e in events_of(breakout_df) if e["type"] == "resistance_breakout"]
    assert events, "close above the 120.5 cluster should register"
    last = events[-1]
    assert last["bar"] == len(df) + 3 and last["evidence"]["close"] == 122 and last["evidence"]["level"] == 120.5
    assert last["evidence"]["touches"] == 3


def test_rsi_divergence_is_dated_at_confirmation():
    # Second low undercuts the first by little after a slow drift, so RSI turns higher.
    down1 = np.linspace(120, 100, 12)
    up = np.linspace(100, 112, 10)[1:]
    down2 = np.linspace(112, 99, 20)[1:]
    tail = np.linspace(99, 106, 10)[1:]
    close = np.r_[np.full(10, 120.0), down1, up, down2, tail]
    df = frame(close)
    events = [e for e in events_of(df) if e["type"] == "rsi_bullish_divergence"]
    assert events
    low_bar = int(np.argmin(close))
    assert events[0]["bar"] == low_bar + tech.PIVOT_K
    assert events[0]["evidence"]["prices"][1] == 99 and events[0]["evidence"]["rsi"][1] > events[0]["evidence"]["rsi"][0]


def test_signal_statistics_exclude_incomplete_windows():
    close = pd.Series(np.arange(100.0, 130.0))
    events = [{"type": "x", "bar": b} for b in (0, 5, 15, 25)]
    stats = tech.signal_statistics(events, close)["x"]
    assert stats["count"] == 4
    # 10-session: bars 0, 5, 15 qualify, returns 10%, 9.5238%, 8.6957%
    assert stats["forward_10"] == {"median_pct": 9.52, "n": 3}
    # 20-session: bars 0, 5 qualify, returns 20% and 19.0476%, median 19.5238%
    assert stats["forward_20"] == {"median_pct": 19.52, "n": 2}
    # bar 19 + 10 = 29 is the last valid index: a window ending exactly on the last bar counts
    assert tech.signal_statistics([{"type": "y", "bar": 19}], close)["y"]["forward_10"]["n"] == 1
    assert tech.signal_statistics([{"type": "y", "bar": 20}], close)["y"]["forward_10"]["n"] == 0


def test_signal_statistics_count_a_consecutive_run_once():
    close = pd.Series(np.arange(100.0, 140.0))
    events = [{"type": "new_52w_low", "bar": b} for b in (3, 4, 5, 9)]
    stats = tech.signal_statistics(events, close)["new_52w_low"]
    assert stats["count"] == 2 and stats["forward_10"]["n"] == 2


def test_breakout_not_refired_by_chop_around_the_level():
    df = zigzag()
    close = np.r_[df["close"].to_numpy(), 115, 118, 121, 119, 122, 119, 123]  # crosses 120.5 three times
    events = [e for e in events_of(frame(close, high=close + 0.5, low=close - 0.5)) if e["type"] == "resistance_breakout"]
    assert [e["bar"] for e in events if e["evidence"]["level"] == 120.5] == [len(df) + 2]


def test_analyze_survives_benchmark_failure_and_uses_private_index_path(monkeypatch):
    df = wave(60)
    df["value"] = np.nan
    df.attrs.update(source="vietcap", warnings=[])
    kinds = []

    def fake_load(symbol, days, kind="stocks"):
        kinds.append((symbol, kind))
        if symbol == tech.BENCHMARK:
            raise core.SourceError("index down")
        return df
    monkeypatch.setattr(tech, "_load", fake_load)
    monkeypatch.setattr(tech, "_crosscheck", lambda s, f: ({"adjusted": None}, ["kiểm tra"], None))
    result = tech.analyze("fpt", chart=False)
    assert kinds == [("FPT", "stocks"), ("VNINDEX", "index")]
    assert result["symbol"] == "FPT" and result["relative_strength"] is None
    assert any("VNINDEX" in w for w in result["warnings"]) and "kiểm tra" in result["warnings"]


@pytest.mark.parametrize("cut", [260, 330, 400, 455, 495])
def test_no_lookahead_future_bars_never_change_past_signals(cut):
    full = wave(500)
    past = events_of(full.iloc[:cut])
    assert past
    later = [e for e in events_of(full) if e["bar"] < cut]
    assert past == later
    # A wildly different future must also leave the past untouched.
    violent = full.copy()
    violent.iloc[cut:, :4] *= 3
    violent.iloc[cut:, 4] *= 50
    assert past == [e for e in events_of(violent) if e["bar"] < cut]


@pytest.mark.parametrize("cut", [120, 300])
def test_no_lookahead_in_indicators(cut):
    full = wave(400)
    short, long_ = tech.compute_indicators(full.iloc[:cut]), tech.compute_indicators(full).iloc[:cut]
    pd.testing.assert_frame_equal(short, long_, check_freq=False)


def test_chart_writes_png_via_agg(tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(core, "REPORTS_DIR", tmp_path)
    df = wave(320)
    df.attrs.update(source="test", warnings=[])
    result = tech._build("AAA", df, None, chart=True)
    assert result["chart"] == str(tmp_path / "charts" / f"AAA_{result['as_of']}.png")
    assert (tmp_path / "charts" / f"AAA_{result['as_of']}.png").read_bytes()[:4] == b"\x89PNG"
    assert tech._build("AAA", df, None, chart=False)["chart"] is None


def test_chart_handles_short_history(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "REPORTS_DIR", tmp_path)
    df = wave(5)
    df.attrs.update(source="test", warnings=[])
    assert tech._build("AAA", df, None, chart=True)["chart"]


# ------------------------------------------------------------ live
def _real_clock():
    return datetime.now(core.VN_TZ)


@pytest.mark.live
@pytest.mark.parametrize("symbol", ["FPT", "VCB"])
def test_live_liquid(symbol, monkeypatch):
    monkeypatch.setattr(core, "now_vn", _real_clock)
    result = tech.analyze(symbol, chart=False)
    assert_pure_json(result)
    assert result["source"] == "vietcap" and result["price_basis"]["adjusted"] is True
    assert result["indicators"]["sma200"] is not None and result["relative_strength"]["1M"]["index_pct"] is not None
    assert not any("Thanh khoản thấp" in w for w in result["warnings"])


@pytest.mark.live
def test_live_fpt_last_bar_and_date_alignment_across_sources(monkeypatch):
    monkeypatch.setattr(core, "now_vn", _real_clock)
    vietcap = tech.get_prices("FPT", 120)
    start = vietcap.index[0]
    kbs = tech._kbs("FPT", 220, "stocks").sort_index()
    kbs = kbs.loc[start:]
    assert list(kbs.index) == list(vietcap.index)
    assert (vietcap.index.weekday < 5).all()
    assert ((vietcap["close"] / kbs["close"] - 1).abs() < 0.005).all()
    dnse = core.fetch_json("https://services.entrade.com.vn/chart-api/v2/ohlcs/stock", params={
        "from": int(start.timestamp()) - 86400, "to": int(_real_clock().timestamp()), "symbol": "FPT", "resolution": "1D"})
    series = pd.Series(dnse["c"], index=tech._vn_dates(dnse["t"])).loc[start:] * 1000
    assert list(series.index) == list(vietcap.index)
    assert ((vietcap["close"] / series - 1).abs() < 0.005).all()


@pytest.mark.live
def test_live_vnindex_private_path():
    index = tech._load(tech.BENCHMARK, 30, kind="index")
    assert len(index) == 30 and index["close"].between(100, 10000).all()


@pytest.mark.live
def test_live_small_cap_illiquid(monkeypatch):
    monkeypatch.setattr(core, "now_vn", _real_clock)
    result = tech.analyze("PMP", days=250, chart=False)
    assert_pure_json(result)
    assert any("Thanh khoản thấp" in w for w in result["warnings"])
    assert any("không có giao dịch" in w for w in result["warnings"])  # Vietcap filler rows removed
    assert (tech.get_prices("PMP", 250)["volume"] > 0).all()


# ------------------------------------------------------------ eight indicator groups (11/11/2026)

def brute_mid(highs, lows, end, n):
    """Midpoint of the n-bar high/low window ending at bar `end`, written out by hand."""
    return (max(highs[end - n + 1:end + 1]) + min(lows[end - n + 1:end + 1])) / 2


def test_ichimoku_lines_and_cloud_against_hand_windows():
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1, 120))
    high, low = close + rng.random(120) * 2, close - rng.random(120) * 2
    ich = tech.ichimoku(frame(close, high, low))
    t = 119
    tenkan, kijun = brute_mid(high, low, t, 9), brute_mid(high, low, t, 26)
    assert ich["tenkan"].iloc[t] == pytest.approx(tenkan)
    assert ich["kijun"].iloc[t] == pytest.approx(kijun)
    assert ich["span_a"].iloc[t] == pytest.approx((tenkan + kijun) / 2)
    assert ich["span_b"].iloc[t] == pytest.approx(brute_mid(high, low, t, 52))
    # The cloud facing bar t was computed 26 bars earlier.
    s = t - 26
    assert ich["cloud_a"].iloc[t] == pytest.approx((brute_mid(high, low, s, 9) + brute_mid(high, low, s, 26)) / 2)
    assert ich["cloud_b"].iloc[t] == pytest.approx(brute_mid(high, low, s, 52))
    assert math.isnan(ich["cloud_b"].iloc[76])  # needs 52 + 26 - 1 bars
    assert not math.isnan(ich["cloud_b"].iloc[77])


def test_ichimoku_group_reads_price_against_cloud(no_network):
    ich = build(frame(np.linspace(10000, 20000, 150)))["ichimoku"]
    assert ich["price_vs_cloud"] == "above_cloud" and ich["tk_relation"] == "tenkan_above_kijun"
    assert ich["chikou_vs_price_26_ago"] == "above" and ich["cloud_bottom"] <= ich["cloud_top"]
    assert build(frame(np.linspace(20000, 10000, 150)))["ichimoku"]["price_vs_cloud"] == "below_cloud"
    short = build(frame(np.linspace(10000, 11000, 60)))["ichimoku"]
    assert short["price_vs_cloud"] is None and short["senkou_span_b"] is None


def test_fibonacci_levels_for_a_rise_and_a_fall():
    up = np.concatenate([np.linspace(100, 80, 30), np.linspace(80, 180, 60), np.linspace(180, 160, 30)])
    fib = tech._fibonacci(frame(up))
    assert (fib["swing_low"], fib["swing_high"], fib["direction"]) == (80, 180, "up")
    assert fib["retracement"] == {"0.236": 156.4, "0.382": 141.8, "0.5": 130.0, "0.618": 118.2, "0.786": 101.4}
    assert fib["extension"] == {"1.272": 207.2, "1.618": 241.8}
    down = np.concatenate([np.linspace(100, 180, 30), np.linspace(180, 80, 60), np.linspace(80, 90, 30)])
    fib = tech._fibonacci(frame(down))
    assert fib["direction"] == "down"
    assert fib["retracement"]["0.382"] == 118.2 and fib["retracement"]["0.618"] == 141.8
    assert fib["extension"] == {"1.272": 52.8, "1.618": 18.2}
    assert tech._fibonacci(frame(np.full(60, 100.0))) is None  # no range, nothing to measure
    assert tech._fibonacci(frame(np.linspace(1, 2, 10))) is None  # too short


def test_fibonacci_looks_only_at_the_recent_window():
    old_peak = np.concatenate([np.linspace(100, 500, 20), np.linspace(500, 100, 20), np.linspace(100, 150, tech.FIB_LOOKBACK)])
    fib = tech._fibonacci(frame(old_peak))
    assert fib["swing_high"] == 150 and fib["lookback_sessions"] == tech.FIB_LOOKBACK


def test_all_eight_groups_are_present_and_pure_json(no_network):
    result = build(wave(400))
    for key in ("ma20", "ma50", "rsi", "macd", "bollinger_bands", "volume_analysis", "ichimoku", "fibonacci", "trade_levels"):
        assert result[key] is not None, key
    assert result["rsi"]["state"] in ("overbought", "oversold", "neutral")
    assert result["macd"]["position"] in ("above_signal", "below_signal", "equal")
    assert result["bollinger_bands"]["squeeze"] in (True, False)
    volume = result["volume_analysis"]
    assert volume["vol_ratio"] == pytest.approx(volume["volume"] / volume["vol_ma20"], abs=1e-3)
    assert result["ma20"]["cross_20_50"]["state"] in ("golden_cross", "death_cross", "sma20_above_sma50", "sma20_below_sma50", "equal")
    assert_pure_json(result)


def test_ma_slope_distance_and_recent_golden_cross(no_network):
    closes = np.concatenate([np.linspace(200, 100, 80), np.linspace(100, 160, 35)])
    ma20 = build(frame(closes))["ma20"]
    window = closes[-20:]
    assert ma20["value"] == pytest.approx(window.mean(), abs=0.01)
    assert ma20["slope_pct_5"] == pytest.approx((window.mean() / closes[-25:-5].mean() - 1) * 100, abs=1e-3)
    assert ma20["distance_pct"] == pytest.approx((closes[-1] / window.mean() - 1) * 100, abs=0.01)
    cross = ma20["cross_20_50"]
    assert cross["last_cross"]["type"] == "golden_cross"
    expected = "golden_cross" if cross["last_cross"]["sessions_ago"] < tech.CROSS_RECENT_SESSIONS else "sma20_above_sma50"
    assert cross["state"] == expected


def groups_for(**over):
    base = {
        "ma20": {"value": 98.0}, "ma50": {"value": 90.0},
        "ichimoku": {"kijun_sen": 96.0, "cloud_bottom": 88.0}, "bollinger_bands": {"upper": 110.0},
        "fibonacci": {"direction": "up", "swing_high": 120.0, "retracement": {"0.382": 99.0, "0.5": 95.0, "0.618": 91.0, "0.786": 86.0},
                      "extension": {"1.272": 125.0, "1.618": 140.0}},
    }
    base.update(over)
    return base


def test_trade_levels_follow_the_written_rules():
    plan = tech.trade_levels(100.0, groups_for(), {"resistance": [{"price": 105.0}]})
    # Within 8% under 100 means >= 92: MA20 98, Kijun 96, Fib .382 99 and .5 95 qualify; Fib .618 at 91 does not.
    assert plan["entry_zone"]["low"] == 95.0 and plan["entry_zone"]["high"] == 100.0
    assert set(plan["entry_zone"]["inputs"]) == {"ma20", "kijun_sen", "fib_0.382", "fib_0.5"}
    assert plan["target_1"] == {"price": 105.0, "source": "resistance"}
    assert plan["target_2"] == {"price": 125.0, "source": "fib_ext_1.272"}
    # Under the zone low 95: MA50 90, cloud bottom 88, Fib .618 91, .786 86; the highest is Fib .618, minus 1%.
    assert plan["stop_loss"]["source"] == "fib_0.618" and plan["stop_loss"]["price"] == pytest.approx(91 * 0.99)
    mid = (95 + 100) / 2
    assert plan["risk_reward"]["entry_mid"] == pytest.approx(mid)
    assert plan["risk_reward"]["ratio"] == pytest.approx(round((105 - mid) / (mid - 91 * 0.99), 2))
    json.dumps(plan, allow_nan=False)


def test_trade_levels_after_a_fall_target_the_retracements():
    groups = groups_for(fibonacci={"direction": "down", "swing_high": 130.0,
                                   "retracement": {"0.236": 97.0, "0.382": 104.0, "0.5": 112.0, "0.618": 118.0, "0.786": 126.0},
                                   "extension": {"1.272": 70.0, "1.618": 60.0}})
    plan = tech.trade_levels(100.0, groups, {"resistance": []})
    assert plan["target_1"] == {"price": 110.0, "source": "bb_upper"}  # nearest above the close among resistance, Bollinger upper, swing high
    assert plan["target_2"] == {"price": 112.0, "source": "fib_0.5"}  # never an extension below the low


def test_trade_levels_say_so_instead_of_inventing():
    far = groups_for(ma20={"value": 80.0}, ichimoku={"kijun_sen": 70.0, "cloud_bottom": None}, bollinger_bands={"upper": None},
                     fibonacci={"direction": "up", "swing_high": 99.0,
                                "retracement": {"0.382": 120.0, "0.5": 130.0, "0.618": 140.0, "0.786": 150.0},
                                "extension": {"1.272": None, "1.618": None}})
    plan = tech.trade_levels(100.0, far, {"resistance": []})
    assert plan["entry_zone"] is None and plan["stop_loss"] is None and plan["risk_reward"] is None
    assert plan["target_1"] is None and plan["target_2"] is None and plan["notes"]
