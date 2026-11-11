"""Prices, indicators, levels, descriptive signals and the chart. No buy/sell advice.

Verified on 2026-11-07 against VNDirect close/adClose over FPT, HPG and VCB ex-dates:
Vietcap, KBS and DNSE prices are all ADJUSTED for splits and dividends (they track adClose,
never the raw close). Volume is NOT adjusted. Prices are therefore not on the tick grid and
levels are left unsnapped. Vietcap emits flat zero-volume rows on no-trade days; KBS omits them.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from . import core

VIETCAP_URL = "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart"
KBS_URL = "https://kbbuddywts.kbsec.com.vn/iis-server/investment/{kind}/{symbol}/data_day"
VNDIRECT_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
BENCHMARK = "VNINDEX"
COLUMNS = ["open", "high", "low", "close", "volume"]

PIVOT_K = 5                # sessions on each side of a swing pivot
RECENT_SESSIONS = 30       # window of the "signals" list
ILLIQUID_VALUE_VND = 1e9   # heuristic: 20-session average traded value per session
SOFT_STALE_SESSIONS = 5    # Tet can close the market for about this many weekdays
# Largest plausible one-session close-to-close fall per exchange. UPCoM references the average
# price rather than the prior close, so its observed moves run far beyond the 15% band.
DROP_LIMIT = {"HOSE": 0.08, "HNX": 0.11, "UPCOM": 0.30}
DROP_LIMIT_UNKNOWN = 0.16

MIN_BARS = {
    "sma20": 20, "sma50": 50, "sma200": 200, "ema12": 12, "ema26": 26, "macd": 26, "macd_signal": 34,
    "macd_hist": 34, "rsi14": 15, "stoch_k": 16, "stoch_d": 18, "bb_upper": 20, "bb_middle": 20,
    "bb_lower": 20, "bb_percent_b": 20, "bb_bandwidth": 20, "atr14": 14, "adx14": 28, "plus_di14": 15,
    "minus_di14": 15, "obv": 1, "mfi14": 15, "volume_sma20": 20, "volume_ratio20": 20,
}


# ---------------------------------------------------------------- prices
def _vn_dates(epochs: Any) -> pd.DatetimeIndex:
    """Epoch seconds to the Vietnamese calendar date (UTC+7), naive and normalised."""
    stamps = pd.to_datetime(pd.Series(list(epochs)).astype("int64"), unit="s")
    return pd.DatetimeIndex(stamps + pd.Timedelta(hours=7)).normalize()


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS + ["value"], index=pd.DatetimeIndex([]), dtype=float)


def _parse_vietcap(payload: Any) -> pd.DataFrame:
    row = payload[0] if isinstance(payload, list) and payload else None
    if not row or not row.get("t"):
        return _empty_frame()
    frame = pd.DataFrame({
        "open": row["o"], "high": row["h"], "low": row["l"], "close": row["c"], "volume": row["v"],
    }, index=_vn_dates(row["t"]), dtype=float)
    value = row.get("accumulatedValue")  # millions of VND, checked against VNDirect nmValue
    frame["value"] = pd.to_numeric(pd.Series(value, index=frame.index), errors="coerce") * 1e6 if value else np.nan
    return frame


def _parse_kbs(payload: Any) -> pd.DataFrame:
    rows = (payload or {}).get("data_day") or []
    if not rows:
        return _empty_frame()
    # "2026-11-06 07:00" is already local time; the date part is the session date.
    index = pd.DatetimeIndex(pd.to_datetime([str(r["t"])[:10] for r in rows]))
    frame = pd.DataFrame({
        "open": [r.get("o") for r in rows], "high": [r.get("h") for r in rows],
        "low": [r.get("l") for r in rows], "close": [r.get("c") for r in rows],
        "volume": [r.get("v") for r in rows], "value": [r.get("va") for r in rows],
    }, index=index)
    return frame.apply(pd.to_numeric, errors="coerce")  # the index feed mixes strings and numbers


def _clean(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Sort, de-duplicate and drop unusable rows. Never fills or synthesises a bar."""
    warnings: list[str] = []
    frame = raw[~raw.index.duplicated(keep="last")].sort_index()
    prices = frame[["open", "high", "low", "close"]]
    bad = prices.isna().any(axis=1) | (prices <= 0).any(axis=1) | (frame["high"] < frame["low"])
    if bad.any():
        warnings.append(f"Bỏ {int(bad.sum())} dòng có giá không hợp lệ.")
    frame = frame[~bad]
    flat = (frame["open"] == frame["high"]) & (frame["high"] == frame["low"]) & (frame["low"] == frame["close"])
    no_volume = frame["volume"].fillna(0) <= 0
    filler = no_volume & flat
    if filler.any():
        warnings.append(f"Bỏ {int(filler.sum())} dòng không có giao dịch (khối lượng 0, giá đi ngang); "
                        "chỉ báo tính theo phiên có giao dịch thực.")
    odd = no_volume & ~flat
    if odd.any():
        warnings.append(f"{int(odd.sum())} dòng có biên độ giá nhưng khối lượng 0, vẫn giữ lại.")
    frame = frame[~filler].copy()
    frame["volume"] = frame["volume"].fillna(0.0)
    weekend = frame.index.weekday >= 5
    if weekend.any():
        warnings.append(f"{int(weekend.sum())} phiên rơi vào cuối tuần, nghi lệch múi giờ của nguồn.")
    return frame, warnings


def _vietcap(symbol: str, days: int) -> pd.DataFrame:
    body = {"timeFrame": "ONE_DAY", "symbols": [symbol], "to": int(core.now_vn().timestamp()), "countBack": days}
    return _parse_vietcap(core.fetch_json(VIETCAP_URL, method="POST", headers=core.VIETCAP_HEADERS, json_body=body))


def _kbs(symbol: str, days: int, kind: str) -> pd.DataFrame:
    today = core.now_vn().date()
    params = {"sdate": (today - timedelta(days=days)).strftime("%d-%m-%Y"), "edate": today.strftime("%d-%m-%Y")}
    return _parse_kbs(core.fetch_json(KBS_URL.format(kind=kind, symbol=symbol), params=params))


def _load(symbol: str, days: int, kind: str = "stocks") -> pd.DataFrame:
    """Vietcap then KBS. `symbol` is a validated ticker or the BENCHMARK constant."""
    if days <= 0:
        raise ValueError("days phải là số dương")
    warnings: list[str] = []
    try:
        raw = _vietcap(symbol, days)
        cleaned, notes = _clean(raw)
        if len(cleaned) < days and len(raw) >= days:  # filler rows consumed the countBack
            raw = _vietcap(symbol, days * 5)
            cleaned, notes = _clean(raw)
        if cleaned.empty:
            raise core.SourceError(f"Vietcap không có dữ liệu cho {symbol}")
        source = "vietcap"
    except (core.SourceError, KeyError, TypeError, ValueError, IndexError) as exc:
        warnings.append(f"Vietcap lỗi ({exc}); chuyển sang KBS.")
        try:  # KBS takes a calendar range and has no filler rows
            cleaned, notes = _clean(_kbs(symbol, int(days * 1.6) + 10, kind))
        except (KeyError, TypeError, ValueError, AttributeError) as kbs_exc:
            raise core.SourceError(f"KBS trả dữ liệu lỗi cho {symbol}: {kbs_exc}") from kbs_exc
        if cleaned.empty:
            raise core.SourceError(f"Không có dữ liệu giá cho {symbol} từ Vietcap và KBS")
        source = "kbs"
    frame = cleaned.tail(days).copy()
    frame.index.name = "date"
    frame.attrs.update(source=source, warnings=warnings + notes)
    return frame


def get_prices(symbol: str, days: int = 500) -> pd.DataFrame:
    """Last `days` traded sessions, adjusted prices in VND, ascending; volume is not adjusted."""
    frame = _load(core.validate_symbol(symbol), days)
    result = frame[COLUMNS].copy()
    result.attrs.update(frame.attrs)
    return result


def _expected_session(now: datetime) -> pd.Timestamp:
    """Latest weekday whose session should be published (today only after 15:30 VN)."""
    day = pd.Timestamp(now.date())
    if not (day.weekday() < 5 and (now.hour, now.minute) >= (15, 30)):
        day -= pd.Timedelta(days=1)
    while day.weekday() >= 5:
        day -= pd.Timedelta(days=1)
    return day


def _staleness(last: pd.Timestamp, now: datetime) -> list[str]:
    if last.date() > now.date():
        return [f"Phiên cuối {last.date()} nằm sau ngày hiện tại, nghi lệch ngày."]
    if last.date() == now.date() and (now.hour, now.minute) < (15, 0):
        return [f"Phiên {last.date()} đang diễn ra: nến cuối chưa hoàn tất, chỉ báo và tín hiệu có thể đổi."]
    behind = int(np.busday_count(last.date(), _expected_session(now).date()))
    if behind <= 0:
        return []
    if behind <= SOFT_STALE_SESSIONS:
        return [f"Phiên cuối {last.date()} chậm {behind} ngày làm việc so với lịch: "
                "có thể do nghỉ lễ, không có giao dịch hoặc nguồn chậm."]
    return [f"Dữ liệu cũ: phiên cuối {last.date()} chậm {behind} ngày làm việc; "
            "mã có thể bị ngừng giao dịch, hủy niêm yết hoặc thanh khoản rất thấp."]


def _crosscheck(symbol: str, frame: pd.DataFrame) -> tuple[dict[str, Any], list[str], str | None]:
    """Compare the series with VNDirect close/adClose; list ex-dates inside the window."""
    basis: dict[str, Any] = {"adjusted": None, "checked_against": None, "match_adjusted_pct": None,
                             "match_raw_pct": None, "ex_dates": []}
    try:
        payload = core.fetch_json(VNDIRECT_URL, params={
            "sort": "date", "q": f"code:{symbol}~date:gte:{frame.index[0].date()}", "size": 3000, "page": 1})
        ref = pd.DataFrame(payload["data"])
        if ref.empty:
            raise core.SourceError("không có dòng dữ liệu")
        ref.index = pd.DatetimeIndex(pd.to_datetime(ref["date"]))
        ref = ref.sort_index()
        adjusted, raw_close = ref["adClose"].astype(float) * 1000, ref["close"].astype(float) * 1000
    except (core.SourceError, KeyError, TypeError, ValueError) as exc:
        return basis, [f"Không đối chiếu được giá điều chỉnh với VNDirect ({exc})."], None
    floor = str(ref["floor"].iloc[-1]).upper() if "floor" in ref else None
    basis["checked_against"] = "VNDirect adClose/close"
    ratio = adjusted / raw_close
    ex_dates = ratio.index[(ratio / ratio.shift(1) - 1).abs() > 0.002]
    basis["ex_dates"] = [d.date().isoformat() for d in ex_dates]
    common = frame.index.intersection(ref.index)
    if not len(common):
        return basis, ["Không có ngày chung với VNDirect để đối chiếu."], floor
    close = frame.loc[common, "close"]
    adj_hit = float(((close / adjusted.loc[common] - 1).abs() < 0.01).mean())
    raw_hit = float(((close / raw_close.loc[common] - 1).abs() < 0.01).mean())
    basis["match_adjusted_pct"], basis["match_raw_pct"] = round(adj_hit * 100, 1), round(raw_hit * 100, 1)
    warnings: list[str] = []
    if adj_hit >= 0.95:
        basis["adjusted"] = True
    elif raw_hit > adj_hit:
        basis["adjusted"] = False
        warnings.append(f"Chuỗi giá có vẻ CHƯA điều chỉnh (khớp giá thô {raw_hit:.0%}, khớp adClose {adj_hit:.0%}): "
                        "chỉ báo sai qua các ngày giao dịch không hưởng quyền.")
    else:
        warnings.append(f"Chuỗi giá khớp adClose của VNDirect chỉ {adj_hit:.0%}: chưa xác định được cơ sở điều chỉnh.")
    if len(ex_dates):
        warnings.append(f"Có {len(ex_dates)} ngày điều chỉnh trong cửa sổ ({', '.join(basis['ex_dates'][-3:])} là gần nhất): "
                        "giá lịch sử, ngưỡng và đỉnh/đáy 52 tuần là giá điều chỉnh, không phải giá khớp thực tế; "
                        "khối lượng không được điều chỉnh nên OBV/MFI có sai số qua các ngày này.")
    return basis, warnings, floor


def _gap_warnings(frame: pd.DataFrame, floor: str | None) -> list[str]:
    """One-session falls beyond the price band are what an unadjusted ex-date looks like."""
    limit = DROP_LIMIT.get(floor or "", DROP_LIMIT_UNKNOWN)
    change = frame["close"].pct_change()
    consecutive = pd.Series(frame.index, index=frame.index).diff().dt.days <= 4
    gaps = change[(change < -limit) & consecutive]
    if gaps.empty:
        return []
    listed = ", ".join(f"{d.date()} ({v:.1%})" for d, v in gaps.tail(3).items())
    return [f"{len(gaps)} phiên giảm vượt {limit:.0%} so với phiên liền trước, gần nhất: {listed}. "
            "Có thể là ngày giao dịch không hưởng quyền chưa được điều chỉnh hoặc lỗi dữ liệu."]


# ---------------------------------------------------------------- indicators
def _recursive(series: pd.Series, n: int, alpha: float) -> pd.Series:
    """Seed with the mean of the first n valid values, then y = y_prev + alpha * (x - y_prev).

    alpha = 2/(n+1) gives the EMA, alpha = 1/n gives Wilder smoothing. A gap later in the
    series holds the state and emits the held value.
    """
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=series.index)
    start = valid[n - 1]
    state = values[valid[:n]].mean()
    out[start] = state
    for i in range(start + 1, len(values)):
        if not np.isnan(values[i]):
            state += alpha * (values[i] - state)
        out[i] = state
    return pd.Series(out, index=series.index)


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return _recursive(series, n, 2 / (n + 1))


def wilder(series: pd.Series, n: int) -> pd.Series:
    return _recursive(series, n, 1 / n)


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """num/den, NaN where the denominator is zero."""
    return num / den.where(den != 0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain, loss = wilder(diff.clip(lower=0), n), wilder((-diff).clip(lower=0), n)
    return 100 * _ratio(gain, gain + loss)  # same as 100 - 100/(1+RS), undefined when flat


def stochastic(df: pd.DataFrame, n: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    low, high = df["low"].rolling(n, min_periods=n).min(), df["high"].rolling(n, min_periods=n).max()
    slow_k = sma(100 * _ratio(df["close"] - low, high - low), k)
    return pd.DataFrame({"stoch_k": slow_k, "stoch_d": sma(slow_k, d)})


def bollinger(close: pd.Series, n: int = 20, width: float = 2.0) -> pd.DataFrame:
    """Population standard deviation, as Bollinger defines the bands."""
    mid, std = sma(close, n), close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + width * std, mid - width * std
    return pd.DataFrame({"bb_upper": upper, "bb_middle": mid, "bb_lower": lower,
                         "bb_percent_b": _ratio(close - lower, upper - lower),
                         "bb_bandwidth": _ratio(upper - lower, mid)})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up, down = df["high"].diff(), -df["low"].diff()
    plus = up.where((up > down) & (up > 0), 0.0).where(up.notna())
    minus = down.where((down > up) & (down > 0), 0.0).where(down.notna())
    tr = true_range(df).where(up.notna())  # the DM series starts at the second bar
    smooth_tr = wilder(tr, n)
    plus_di, minus_di = 100 * _ratio(wilder(plus, n), smooth_tr), 100 * _ratio(wilder(minus, n), smooth_tr)
    total = plus_di + minus_di
    dx = 100 * _ratio((plus_di - minus_di).abs(), total)
    dx = dx.where(~((total == 0) & plus_di.notna()), 0.0)  # range but no directional move
    return pd.DataFrame({"plus_di14": plus_di, "minus_di14": minus_di, "adx14": wilder(dx, n)})


def obv(df: pd.DataFrame) -> pd.Series:
    """Starts at 0 on the first bar; only changes in OBV carry meaning."""
    return (np.sign(df["close"].diff()).fillna(0.0) * df["volume"]).cumsum()


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    flow, change = typical * df["volume"], typical.diff()
    pos = flow.where(change > 0, 0.0).where(change.notna()).rolling(n, min_periods=n).sum()
    neg = flow.where(change < 0, 0.0).where(change.notna()).rolling(n, min_periods=n).sum()
    return 100 * _ratio(pos, pos + neg)


def rolling_52w(df: pd.DataFrame) -> pd.DataFrame:
    """365-calendar-day high/low, null until the data spans a full 52 weeks."""
    enough = df.index >= df.index[0] + pd.Timedelta(days=364)
    high = df["high"].rolling("365D").max().where(enough)
    low = df["low"].rolling("365D").min().where(enough)
    return pd.DataFrame({"high_52w": high, "low_52w": low})


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    out = pd.DataFrame({"sma20": sma(close, 20), "sma50": sma(close, 50), "sma200": sma(close, 200),
                        "ema12": ema(close, 12), "ema26": ema(close, 26), "rsi14": rsi(close),
                        "atr14": atr(df), "obv": obv(df), "mfi14": mfi(df),
                        "volume_sma20": sma(df["volume"], 20)}, index=df.index)
    out["volume_ratio20"] = _ratio(df["volume"], out["volume_sma20"])
    return pd.concat([out, macd(close), stochastic(df), bollinger(close), adx(df), rolling_52w(df)], axis=1)


# ---------------------------------------------------------------- levels
def _pivots(df: pd.DataFrame, k: int = PIVOT_K) -> tuple[list[int], list[int]]:
    """Fractal swing points: strictly beyond the k bars before, at least level with the k after."""
    high, low = df["high"].to_numpy(float), df["low"].to_numpy(float)
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if high[i] > high[i - k:i].max() and high[i] >= high[i + 1:i + k + 1].max():
            highs.append(i)
        if low[i] < low[i - k:i].min() and low[i] <= low[i + 1:i + k + 1].min():
            lows.append(i)
    return highs, lows


def _cluster(points: list[tuple[float, int]], tolerance: float) -> list[dict[str, Any]]:
    """Group (price, bar) pivots whose price stays within tolerance of the cluster mean."""
    clusters: list[list[tuple[float, int]]] = []
    for point in sorted(points):
        if clusters and point[0] - np.mean([p for p, _ in clusters[-1]]) <= tolerance:
            clusters[-1].append(point)
        else:
            clusters.append([point])
    return [{"price": float(np.mean([p for p, _ in c])), "touches": len(c), "last": max(b for _, b in c)}
            for c in clusters]


def _levels_at(t: int, df: pd.DataFrame, atr14: pd.Series, highs: list[int], lows: list[int]) -> list[dict[str, Any]]:
    """Clusters using only pivots already confirmed at bar t and the ATR known at bar t."""
    if t < 0 or math.isnan(atr14.iloc[t]):
        return []
    points = [(float(df["high"].iloc[i]), i) for i in highs if i + PIVOT_K <= t]
    points += [(float(df["low"].iloc[i]), i) for i in lows if i + PIVOT_K <= t]
    return _cluster(points, max(0.5 * float(atr14.iloc[t]), 0.005 * float(df["close"].iloc[t])))


def find_levels(df: pd.DataFrame, ind: pd.DataFrame, per_side: int = 5) -> dict[str, Any]:
    highs, lows = _pivots(df)
    clusters = _levels_at(len(df) - 1, df, ind["atr14"], highs, lows)
    close, atr_now = float(df["close"].iloc[-1]), ind["atr14"].iloc[-1]

    def row(level: dict[str, Any]) -> dict[str, Any]:
        gap = level["price"] - close
        return {"price": _num(level["price"]), "touches": level["touches"],
                "last_touch": _date(df.index[level["last"]]), "distance_pct": _num(gap / close * 100),
                "distance_atr": _num(gap / atr_now) if atr_now and atr_now > 0 else None}

    support = sorted((c for c in clusters if c["price"] < close), key=lambda c: -c["price"])[:per_side]
    resistance = sorted((c for c in clusters if c["price"] >= close), key=lambda c: c["price"])[:per_side]
    return {"support": [row(c) for c in support], "resistance": [row(c) for c in resistance],
            "method": f"Đỉnh/đáy swing xác nhận bởi {PIVOT_K} phiên hai bên, gom cụm trong max(0,5×ATR14; 0,5% giá). "
                      "touches là số pivot trong cụm. Giá điều chỉnh, không làm tròn theo bước giá."}


# ---------------------------------------------------------------- signals
def _crossed_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def detect_signals(df: pd.DataFrame, ind: pd.DataFrame) -> list[dict[str, Any]]:
    """Every event in the history, ascending. Each uses only data up to its own date."""
    events: list[tuple[int, str, str, dict[str, Any]]] = []
    positions = pd.Series(range(len(df)), index=df.index)
    close, high, low = df["close"], df["high"], df["low"]

    def fire(mask: pd.Series, kind: str, text: str, *fields: str) -> None:
        for t in positions[mask.fillna(False).astype(bool)]:
            source = {f: (ind[f].iloc[t] if f in ind else df[f].iloc[t]) for f in fields}
            events.append((int(t), kind, text, {f: _num(v) for f, v in source.items()}))

    for fast, slow, up, down in ((50, 200, "golden_cross", "death_cross"), (20, 50, "sma20_cross_above_sma50", "sma20_cross_below_sma50")):
        a, b = ind[f"sma{fast}"], ind[f"sma{slow}"]
        fire(_crossed_up(a, b), up, f"SMA{fast} cắt lên SMA{slow}.", f"sma{fast}", f"sma{slow}")
        fire(_crossed_up(b, a), down, f"SMA{fast} cắt xuống SMA{slow}.", f"sma{fast}", f"sma{slow}")
    fire(_crossed_up(ind["macd"], ind["macd_signal"]), "macd_cross_up", "MACD cắt lên đường tín hiệu.", "macd", "macd_signal")
    fire(_crossed_up(ind["macd_signal"], ind["macd"]), "macd_cross_down", "MACD cắt xuống đường tín hiệu.", "macd", "macd_signal")
    fire((ind["rsi14"] > 70) & (ind["rsi14"].shift(1) <= 70), "rsi_overbought", "RSI14 vượt lên trên 70.", "rsi14")
    fire((ind["rsi14"] < 30) & (ind["rsi14"].shift(1) >= 30), "rsi_oversold", "RSI14 giảm xuống dưới 30.", "rsi14")
    width = ind["bb_bandwidth"]
    squeeze = (width > 0) & (width <= width.rolling(120, min_periods=120).min())
    fire(squeeze & ~squeeze.shift(1, fill_value=False), "bollinger_squeeze",
         "Độ rộng Bollinger chạm mức thấp nhất 120 phiên.", "bb_bandwidth")
    fire((close > ind["bb_upper"]) & (close.shift(1) <= ind["bb_upper"].shift(1)), "bollinger_break_up",
         "Giá đóng cửa vượt trên dải Bollinger trên.", "close", "bb_upper")
    fire((close < ind["bb_lower"]) & (close.shift(1) >= ind["bb_lower"].shift(1)), "bollinger_break_down",
         "Giá đóng cửa xuống dưới dải Bollinger dưới.", "close", "bb_lower")
    fire((ind["volume_ratio20"] >= 2) & (ind["volume_ratio20"].shift(1) < 2), "volume_spike",
         "Khối lượng từ 2 lần trung bình 20 phiên trở lên.", "volume", "volume_sma20", "volume_ratio20")
    fire(high > ind["high_52w"].shift(1), "new_52w_high", "Lập đỉnh mới 52 tuần (giá điều chỉnh).", "high")
    fire(low < ind["low_52w"].shift(1), "new_52w_low", "Lập đáy mới 52 tuần (giá điều chỉnh).", "low")

    highs, lows = _pivots(df)
    for pivots, series, kind, text, is_low in ((lows, low, "rsi_bullish_divergence", "Giá tạo đáy thấp hơn trong khi RSI tạo đáy cao hơn.", True),
                                               (highs, high, "rsi_bearish_divergence", "Giá tạo đỉnh cao hơn trong khi RSI tạo đỉnh thấp hơn.", False)):
        for prev, cur in zip(pivots, pivots[1:]):
            r_prev, r_cur = ind["rsi14"].iloc[prev], ind["rsi14"].iloc[cur]
            if cur - prev > 60 or math.isnan(r_prev) or math.isnan(r_cur):
                continue
            price_extends = series.iloc[cur] < series.iloc[prev] if is_low else series.iloc[cur] > series.iloc[prev]
            rsi_disagrees = r_cur > r_prev if is_low else r_cur < r_prev
            if price_extends and rsi_disagrees:  # dated when the later pivot is confirmed
                events.append((cur + PIVOT_K, kind, text, {
                    "pivot_dates": [_date(df.index[prev]), _date(df.index[cur])],
                    "prices": [_num(series.iloc[prev]), _num(series.iloc[cur])],
                    "rsi": [_num(r_prev), _num(r_cur)]}))

    for t in range(1, len(df)):
        known = _levels_at(t - 1, df, ind["atr14"], highs, lows)
        before = close.iloc[max(t - 5, 0):t].max()  # chop around one level is not a fresh breakout
        crossed = [c for c in known if c["touches"] >= 2 and before <= c["price"] < close.iloc[t]]
        if crossed:
            level = max(crossed, key=lambda c: c["price"])
            events.append((t, "resistance_breakout", "Giá đóng cửa vượt ngưỡng kháng cự đã phát hiện, sau ít nhất 5 phiên đóng cửa không cao hơn ngưỡng.",
                           {"close": _num(close.iloc[t]), "level": _num(level["price"]), "touches": level["touches"]}))

    events.sort(key=lambda e: (e[0], e[1]))
    return [{"bar": t, "type": kind, "date": _date(df.index[t]), "statement": text, "evidence": evidence}
            for t, kind, text, evidence in events]


def signal_statistics(events: list[dict[str, Any]], close: pd.Series) -> dict[str, Any]:
    """Median forward 10/20-session return per type; a window past the last bar is excluded.

    A run of the same event on consecutive bars (a stretch of new lows) counts once, at its start.
    """
    values = close.to_numpy(float)
    grouped: dict[str, list[int]] = {}
    latest: dict[str, int] = {}
    for event in events:
        kind, bar = event["type"], event["bar"]
        if latest.get(kind) != bar - 1:
            grouped.setdefault(kind, []).append(bar)
        latest[kind] = bar
    result = {}
    for kind, bars in sorted(grouped.items()):
        row: dict[str, Any] = {"count": len(bars)}
        for horizon in (10, 20):
            returns = [values[b + horizon] / values[b] - 1 for b in bars if b + horizon < len(values)]
            row[f"forward_{horizon}"] = {"median_pct": _num(float(np.median(returns)) * 100) if returns else None,
                                         "n": len(returns)}
        result[kind] = row
    return result


# ---------------------------------------------------------------- derived sections
def _num(value: Any, digits: int = 2) -> float | None:
    """Plain rounded float, or None for missing and non-finite values."""
    if value is None:
        return None
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def _date(stamp: pd.Timestamp) -> str:
    return stamp.date().isoformat()


def _return_since(close: pd.Series, target: pd.Timestamp) -> tuple[float | None, str | None]:
    """Percent change from the last bar on or before target; null if that bar is over 10 days older."""
    earlier = close.loc[:target]
    if earlier.empty or (target - earlier.index[-1]).days > 10:
        return None, None
    return _num((close.iloc[-1] / earlier.iloc[-1] - 1) * 100), _date(earlier.index[-1])


def _price_changes(close: pd.Series) -> dict[str, Any]:
    last = close.index[-1]
    previous = close.index[-2] if len(close) > 1 else None
    targets = {"1D": previous, "1W": last - pd.Timedelta(days=7), "1M": last - pd.DateOffset(months=1),
               "3M": last - pd.DateOffset(months=3), "YTD": pd.Timestamp(year=last.year - 1, month=12, day=31),
               "1Y": last - pd.DateOffset(years=1)}
    out = {}
    for label, target in targets.items():
        pct, since = _return_since(close, target) if target is not None else (None, None)
        out[label] = {"pct": pct, "from": since}
    return out


def _trend(df: pd.DataFrame, ind: pd.DataFrame) -> dict[str, Any]:
    close = float(df["close"].iloc[-1])

    def slope(column: str, lag: int) -> float | None:
        series = ind[column]
        return _num(series.iloc[-1] - series.iloc[-1 - lag]) if len(series) > lag else None

    def state(conditions: list[float | None]) -> str | None:
        """Each condition is a signed gap; all positive is up, all negative is down."""
        if any(c is None or math.isnan(c) for c in conditions):
            return None
        return "up" if all(c > 0 for c in conditions) else "down" if all(c < 0 for c in conditions) else "mixed"

    def latest(column: str) -> float:
        return float(ind[column].iloc[-1])

    s20, s50, s200 = slope("sma20", 5), slope("sma50", 10), slope("sma200", 20)
    short = state([close - latest("sma20"), s20])
    medium = state([close - latest("sma50"), latest("sma20") - latest("sma50"), s50])
    long_ = state([close - latest("sma200"), latest("sma50") - latest("sma200"), s200])
    adx_now, plus, minus = _num(latest("adx14")), _num(latest("plus_di14")), _num(latest("minus_di14"))
    strength = None if adx_now is None else "trending" if adx_now >= 25 else "weak" if adx_now <= 20 else "developing"
    direction = None if plus is None or minus is None else "plus_di_above" if plus > minus else "minus_di_above" if minus > plus else "equal"
    return {
        "short": {"state": short, "rule": "up nếu close>SMA20 và SMA20 tăng so với 5 phiên trước; down nếu ngược lại cả hai; còn lại mixed.",
                  "inputs": {"close": _num(close), "sma20": _num(latest("sma20")), "sma20_change_5": s20}},
        "medium": {"state": medium, "rule": "up nếu close>SMA50, SMA20>SMA50 và SMA50 tăng so với 10 phiên trước; down nếu ngược lại cả ba; còn lại mixed.",
                   "inputs": {"close": _num(close), "sma20": _num(latest("sma20")), "sma50": _num(latest("sma50")), "sma50_change_10": s50}},
        "long": {"state": long_, "rule": "up nếu close>SMA200, SMA50>SMA200 và SMA200 tăng so với 20 phiên trước; down nếu ngược lại cả ba; còn lại mixed.",
                 "inputs": {"close": _num(close), "sma50": _num(latest("sma50")), "sma200": _num(latest("sma200")), "sma200_change_20": s200}},
        "strength": {"state": strength, "direction": direction,
                     "rule": "ADX14>=25 trending, <=20 weak, còn lại developing; direction so sánh +DI với -DI.",
                     "inputs": {"adx14": adx_now, "plus_di14": plus, "minus_di14": minus}},
    }


def _indicator_values(df: pd.DataFrame, ind: pd.DataFrame) -> tuple[dict[str, Any], list[str]]:
    last = ind.iloc[-1]
    close, n = float(df["close"].iloc[-1]), len(df)
    digits = {"bb_percent_b": 4, "bb_bandwidth": 4, "obv": 0, "volume_sma20": 0, "volume_ratio20": 3}
    values = {name: _num(last[name], digits.get(name, 2)) for name in MIN_BARS}
    warnings = []
    short = sorted((name for name in MIN_BARS if values[name] is None and n < MIN_BARS[name]), key=MIN_BARS.get)
    undefined = [name for name in MIN_BARS if values[name] is None and n >= MIN_BARS[name]]
    if short:
        needs = ", ".join(f"{name} (cần {MIN_BARS[name]})" for name in short)
        warnings.append(f"Thiếu lịch sử, chỉ có {n} phiên: {needs} trả null.")
    if undefined:
        warnings.append(f"Không xác định được vì mẫu số bằng 0 (giá đi ngang): {', '.join(undefined)} trả null.")
    high, low = last["high_52w"], last["low_52w"]
    values.update(high_52w=_num(high), low_52w=_num(low),
                  distance_from_high_52w_pct=_num((close / high - 1) * 100) if pd.notna(high) else None,
                  distance_from_low_52w_pct=_num((close / low - 1) * 100) if pd.notna(low) else None)
    if pd.isna(high):
        warnings.append(f"Dữ liệu chưa đủ 52 tuần (từ {_date(df.index[0])}): đỉnh/đáy 52 tuần trả null.")
    return values, warnings


def _liquidity(frame: pd.DataFrame) -> tuple[float | None, list[str]]:
    recent = frame.tail(20)
    if recent.empty:
        return None, []
    value = recent["value"].fillna(recent["close"] * recent["volume"]) if "value" in recent else recent["close"] * recent["volume"]
    average = float(value.mean())
    warnings = []
    if average < ILLIQUID_VALUE_VND:
        warnings.append(f"Thanh khoản thấp: giá trị giao dịch trung bình {len(recent)} phiên khoảng {average / 1e9:.3f} tỷ đồng/phiên; "
                        "giá và chỉ báo dễ bị vài lệnh nhỏ chi phối.")
    span = (recent.index[-1] - recent.index[0]).days
    if len(recent) == 20 and span > 42:
        warnings.append(f"20 phiên có giao dịch gần nhất trải dài {span} ngày: chỉ báo đếm theo phiên có giao dịch, không theo lịch.")
    return average, warnings


def _volatility(close: pd.Series) -> dict[str, Any]:
    returns = np.log(close / close.shift(1)).dropna()
    realised = float(returns.tail(20).std(ddof=1)) * math.sqrt(252) * 100 if len(returns) >= 20 else None
    drawdown = close / close.cummax() - 1
    trough = drawdown.idxmin()
    peak = close.loc[:trough].idxmax()
    return {"realised_20_annualised_pct": _num(realised), "max_drawdown_pct": _num(drawdown.min() * 100),
            "max_drawdown_peak_date": _date(peak), "max_drawdown_trough_date": _date(trough),
            "window_start": _date(close.index[0])}


def _relative_strength(close: pd.Series, index_close: pd.Series | None) -> dict[str, Any] | None:
    if index_close is None:
        return None
    last = close.index[-1]
    index_close = index_close.loc[:last]
    out = {}
    for label, months in (("1M", 1), ("3M", 3), ("6M", 6)):
        target = last - pd.DateOffset(months=months)
        stock, since = _return_since(close, target)
        bench, bench_since = _return_since(index_close, target)
        out[label] = {"stock_pct": stock, "index_pct": bench, "stock_from": since, "index_from": bench_since,
                      "excess_pct": _num(stock - bench) if stock is not None and bench is not None else None}
    return out


# ---------------------------------------------------------------- chart
def render_chart(symbol: str, df: pd.DataFrame, ind: pd.DataFrame, levels: dict[str, Any], sessions: int = 180) -> str:
    """Four stacked panels saved as a PNG through the Agg canvas, no pyplot state."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    ink, muted, grid, surface = "#0b0b0b", "#52514e", "#e6e6e3", "#fcfcfb"
    rise, fall, blue, orange, violet = "#1baf7a", "#e34948", "#2a78d6", "#eb6834", "#6250d6"
    frame, data = df.tail(sessions), ind.tail(sessions)
    x = np.arange(len(frame))
    fig = Figure(figsize=(14, 11), dpi=120, facecolor=surface)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(4, 1, height_ratios=[5, 1.2, 1.4, 1.4], hspace=0.07, left=0.06, right=0.91, top=0.94, bottom=0.06)
    ax_p = fig.add_subplot(gs[0])
    ax_v, ax_r, ax_m = (fig.add_subplot(gs[i], sharex=ax_p) for i in (1, 2, 3))

    up = (frame["close"] >= frame["open"]).to_numpy()
    colors = np.where(up, rise, fall)
    ax_p.vlines(x, frame["low"], frame["high"], colors=colors, linewidth=0.8, zorder=3)
    bottoms = frame[["open", "close"]].min(axis=1).to_numpy()
    heights = (frame["close"] - frame["open"]).abs().to_numpy()
    floor = (frame["high"].max() - frame["low"].min()) * 0.002
    # Hollow rising, filled falling: direction survives colour-blind vision and greyscale print.
    ax_p.bar(x, np.maximum(heights, floor), bottom=bottoms, width=0.65, color=np.where(up, surface, fall),
             edgecolor=colors, linewidth=0.9, zorder=4)
    ax_p.fill_between(x, data["bb_lower"], data["bb_upper"], color="#8a94a6", alpha=0.13, linewidth=0, label="Bollinger(20,2)", zorder=1)
    for column, color, label in (("sma20", blue, "SMA20"), ("sma50", orange, "SMA50"), ("sma200", violet, "SMA200")):
        if data[column].notna().any():
            ax_p.plot(x, data[column], color=color, linewidth=1.5, label=label, zorder=5)
    low_lim, high_lim = float(frame["low"].min()), float(frame["high"].max())
    pad = (high_lim - low_lim) * 0.04 or high_lim * 0.01
    ax_p.set_ylim(low_lim - pad, high_lim + pad)
    for side, color in (("support", rise), ("resistance", fall)):
        for level in levels[side]:
            if low_lim - pad <= level["price"] <= high_lim + pad:
                ax_p.axhline(level["price"], color=color, linewidth=0.9, linestyle=(0, (4, 3)), alpha=0.75, zorder=2)
                ax_p.text(1.004, level["price"], f"{level['price']:,.0f} ×{level['touches']}", transform=ax_p.get_yaxis_transform(),
                          va="center", fontsize=8, color=ink)
    ax_p.legend(loc="lower right", bbox_to_anchor=(1, 1), ncol=4, frameon=False, fontsize=9)
    last = frame.iloc[-1]
    ax_p.set_title(f"{symbol}  {_date(frame.index[-1])}  close {last['close']:,.0f}  "
                   f"(adjusted; dashed = levels, ×touches)", loc="left", fontsize=12, color=ink)
    ax_p.set_ylabel("Price (VND)", fontsize=9, color=muted)

    ax_v.bar(x, frame["volume"] / 1e6, width=0.7, color=colors, alpha=0.75)
    if data["volume_sma20"].notna().any():
        ax_v.plot(x, data["volume_sma20"] / 1e6, color=ink, linewidth=1.1)
    ax_v.set_ylabel("Vol (M)\nline: 20-avg", fontsize=9, color=muted)

    ax_r.plot(x, data["rsi14"], color=blue, linewidth=1.4)
    for mark in (30, 70):
        ax_r.axhline(mark, color=muted, linewidth=0.8, linestyle=(0, (4, 3)))
    ax_r.set_ylim(0, 100)
    ax_r.set_yticks([30, 50, 70])
    ax_r.set_ylabel("RSI 14", fontsize=9, color=muted)

    hist = data["macd_hist"].to_numpy(float)
    ax_m.bar(x, hist, width=0.7, color=np.where(hist >= 0, rise, fall), alpha=0.55)
    ax_m.plot(x, data["macd"], color=blue, linewidth=1.3, label="MACD")
    ax_m.plot(x, data["macd_signal"], color=orange, linewidth=1.3, label="Signal")
    ax_m.axhline(0, color=muted, linewidth=0.8)
    ax_m.legend(loc="lower right", bbox_to_anchor=(1, 1), ncol=2, frameon=False, fontsize=8, borderaxespad=0)
    ax_m.set_ylabel("MACD 12,26,9", fontsize=9, color=muted)

    step = max(len(frame) // 9, 1)
    ax_m.set_xticks(x[::step])
    ax_m.set_xticklabels([d.strftime("%d/%m/%y") for d in frame.index[::step]], fontsize=8.5, color=muted)
    ax_p.set_xlim(-1, len(frame))
    for ax in (ax_p, ax_v, ax_r, ax_m):
        ax.grid(True, color=grid, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.tick_params(colors=muted, labelsize=8.5, length=0)
        for name, spine in ax.spines.items():
            spine.set_visible(name == "bottom")
            spine.set_color(grid)
        if ax is not ax_m:
            ax.tick_params(labelbottom=False)
    target = core.REPORTS_DIR / "charts" / f"{symbol}_{_date(frame.index[-1])}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target)
    return str(target)


# ---------------------------------------------------------------- eight indicator groups and trade levels
CROSS_RECENT_SESSIONS = 10      # a cross this recent is reported as the current state
SLOPE_SESSIONS = 5
VOLUME_SPIKE_RATIO = 2.0        # same threshold as the volume_spike signal
SQUEEZE_WINDOW = 120            # same rule as the bollinger_squeeze signal
ICHIMOKU = {"tenkan": 9, "kijun": 26, "senkou_b": 52, "displacement": 26}
FIB_LOOKBACK = 120              # sessions searched for the swing high and low
FIB_RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXTENSIONS = (1.272, 1.618)
ENTRY_MAX_BELOW = 0.08          # entry candidates must sit within 8% under the close
STOP_BUFFER = 0.01              # stop sits 1% under the support it protects


def ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    """Lines as computed on each bar. The cloud facing bar t is the spans computed 26 bars earlier."""
    def midpoint(n: int) -> pd.Series:
        return (df["high"].rolling(n, min_periods=n).max() + df["low"].rolling(n, min_periods=n).min()) / 2

    tenkan, kijun = midpoint(ICHIMOKU["tenkan"]), midpoint(ICHIMOKU["kijun"])
    span_a, span_b = (tenkan + kijun) / 2, midpoint(ICHIMOKU["senkou_b"])
    lag = ICHIMOKU["displacement"]
    return pd.DataFrame({"tenkan": tenkan, "kijun": kijun, "span_a": span_a, "span_b": span_b,
                         "cloud_a": span_a.shift(lag), "cloud_b": span_b.shift(lag)}, index=df.index)


def _last_cross(fast: pd.Series, slow: pd.Series, index: pd.Index, up: str, down: str) -> dict[str, Any] | None:
    ups, downs = _crossed_up(fast, slow), _crossed_up(slow, fast)
    hits = [(i, up) for i in np.flatnonzero(ups.to_numpy())] + [(i, down) for i in np.flatnonzero(downs.to_numpy())]
    if not hits:
        return None
    position, kind = max(hits)
    return {"type": kind, "date": _date(index[position]), "sessions_ago": int(len(index) - 1 - position)}


def _relation(a: Any, b: Any, above: str, below: str) -> str | None:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return above if a > b else below if a < b else "equal"


def _ma_group(df: pd.DataFrame, ind: pd.DataFrame, column: str, cross: dict[str, Any]) -> dict[str, Any]:
    series, close = ind[column], float(df["close"].iloc[-1])
    value = series.iloc[-1]
    before = series.iloc[-1 - SLOPE_SESSIONS] if len(series) > SLOPE_SESSIONS else np.nan
    slope = (value / before - 1) * 100 if pd.notna(value) and pd.notna(before) and before else None
    return {"value": _num(value), "slope_pct_5": _num(slope, 3),
            "slope_direction": None if slope is None else "rising" if slope > 0 else "falling" if slope < 0 else "flat",
            "distance_pct": _num((close / value - 1) * 100) if pd.notna(value) and value else None,
            "price_position": _relation(close, value, "above", "below"), "cross_20_50": cross}


def _cross_state(ind: pd.DataFrame, index: pd.Index) -> dict[str, Any]:
    last = _last_cross(ind["sma20"], ind["sma50"], index, "golden_cross", "death_cross")
    relation = _relation(ind["sma20"].iloc[-1], ind["sma50"].iloc[-1], "sma20_above_sma50", "sma20_below_sma50")
    recent = last is not None and last["sessions_ago"] < CROSS_RECENT_SESSIONS
    return {"state": last["type"] if recent else relation, "last_cross": last,
            "rule": f"golden_cross/death_cross nếu SMA20 cắt SMA50 trong {CROSS_RECENT_SESSIONS} phiên gần nhất; nếu không, nêu SMA20 đang trên hay dưới SMA50."}


def _fibonacci(df: pd.DataFrame) -> dict[str, Any] | None:
    window = df.tail(FIB_LOOKBACK)
    if len(window) < 20:
        return None
    high_at, low_at = window["high"].idxmax(), window["low"].idxmin()
    high, low = float(window["high"].max()), float(window["low"].min())
    span = high - low
    if span <= 0:
        return None
    direction = "up" if low_at < high_at else "down"
    # Up: retracements step down from the high, extensions project above it. Down: the mirror image.
    sign, base = (-1, high) if direction == "up" else (1, low)
    origin = low if direction == "up" else high
    retracements = {str(r): _num(base + sign * r * span) for r in FIB_RETRACEMENTS}
    extensions = {str(e): _num(origin - sign * e * span) for e in FIB_EXTENSIONS}
    return {"swing_high": _num(high), "swing_high_date": _date(high_at), "swing_low": _num(low), "swing_low_date": _date(low_at),
            "direction": direction, "retracement": retracements, "extension": extensions, "lookback_sessions": len(window),
            "rule": f"Đỉnh/đáy là giá cao nhất/thấp nhất {FIB_LOOKBACK} phiên gần nhất; direction=up nếu đáy đến trước đỉnh. "
                    "Up: thoái lui = đỉnh − r×biên độ, mở rộng = đáy + e×biên độ. Down: ngược lại."}


def indicator_groups(df: pd.DataFrame, ind: pd.DataFrame) -> dict[str, Any]:
    """The eight groups the trading agents read. Each null means too little history, never a guess."""
    close = float(df["close"].iloc[-1])
    last = ind.iloc[-1]
    cross = _cross_state(ind, df.index)
    rsi_now = _num(last["rsi14"])
    hist = ind["macd_hist"]
    width = ind["bb_bandwidth"]
    squeeze = (width > 0) & (width <= width.rolling(SQUEEZE_WINDOW, min_periods=SQUEEZE_WINDOW).min())
    ich = ichimoku(df)
    now = ich.iloc[-1]
    cloud_ready = pd.notna(now["cloud_a"]) and pd.notna(now["cloud_b"])
    top = max(now["cloud_a"], now["cloud_b"]) if cloud_ready else None
    bottom = min(now["cloud_a"], now["cloud_b"]) if cloud_ready else None
    lag = ICHIMOKU["displacement"]
    past_close = df["close"].iloc[-1 - lag] if len(df) > lag else None
    vol, vol_ma = last["volume_sma20"], ind["volume_sma20"].iloc[-1]
    ratio = _num(last["volume_ratio20"], 3)
    upper, lower = last["bb_upper"], last["bb_lower"]
    return {
        "ma20": _ma_group(df, ind, "sma20", cross),
        "ma50": _ma_group(df, ind, "sma50", cross),
        "rsi": {"value": rsi_now, "period": 14,
                "state": None if rsi_now is None else "overbought" if rsi_now > 70 else "oversold" if rsi_now < 30 else "neutral"},
        "macd": {"macd_line": _num(last["macd"]), "signal_line": _num(last["macd_signal"]), "histogram": _num(last["macd_hist"]),
                 "position": _relation(last["macd"], last["macd_signal"], "above_signal", "below_signal"),
                 "histogram_trend": None if len(hist) < 2 or pd.isna(hist.iloc[-1]) or pd.isna(hist.iloc[-2]) else
                 "expanding" if abs(hist.iloc[-1]) > abs(hist.iloc[-2]) else "contracting",
                 "last_cross": _last_cross(ind["macd"], ind["macd_signal"], df.index, "cross_up", "cross_down"),
                 "params": [12, 26, 9]},
        "bollinger_bands": {"upper": _num(upper), "middle": _num(last["bb_middle"]), "lower": _num(lower),
                            "bandwidth": _num(last["bb_bandwidth"], 4), "percent_b": _num(last["bb_percent_b"], 4),
                            "squeeze": bool(squeeze.iloc[-1]) if pd.notna(width.iloc[-1]) else None,
                            "price_position": None if pd.isna(upper) else "above_upper" if close > upper else "below_lower" if close < lower
                            else "upper_half" if close >= last["bb_middle"] else "lower_half",
                            "rule": f"squeeze khi độ rộng dải là thấp nhất {SQUEEZE_WINDOW} phiên.", "params": [20, 2]},
        "volume_analysis": {"volume": _num(df["volume"].iloc[-1], 0), "vol_ma20": _num(vol_ma, 0), "vol_ratio": ratio,
                            "spike": None if ratio is None else ratio >= VOLUME_SPIKE_RATIO,
                            "rule": f"spike khi vol_ratio ≥ {VOLUME_SPIKE_RATIO}. Khối lượng không được điều chỉnh theo cổ tức/chia tách."},
        "ichimoku": {"tenkan_sen": _num(now["tenkan"]), "kijun_sen": _num(now["kijun"]),
                     "senkou_span_a": _num(now["cloud_a"]), "senkou_span_b": _num(now["cloud_b"]),
                     "cloud_top": _num(top), "cloud_bottom": _num(bottom),
                     "future_span_a": _num(now["span_a"]), "future_span_b": _num(now["span_b"]),
                     "future_cloud": _relation(now["span_a"], now["span_b"], "bullish", "bearish"),
                     "chikou_span": _num(close), "chikou_vs_price_26_ago": _relation(close, past_close, "above", "below"),
                     "price_vs_cloud": None if not cloud_ready else "above_cloud" if close > top else "below_cloud" if close < bottom else "in_cloud",
                     "tk_relation": _relation(now["tenkan"], now["kijun"], "tenkan_above_kijun", "tenkan_below_kijun"),
                     "tk_last_cross": _last_cross(ich["tenkan"], ich["kijun"], df.index, "bullish_cross", "bearish_cross"),
                     "params": [ICHIMOKU["tenkan"], ICHIMOKU["kijun"], ICHIMOKU["senkou_b"]],
                     "rule": "senkou_span_a/b là mây đối diện phiên hiện tại (tính 26 phiên trước); future_span_a/b là mây sẽ vẽ 26 phiên tới."},
        "fibonacci": _fibonacci(df),
    }


def trade_levels(close: float, groups: dict[str, Any], levels: dict[str, Any]) -> dict[str, Any]:
    """Entry zone, targets, stop and R:R by fixed rules, so the trading agents quote numbers instead of inventing them."""
    fib = groups["fibonacci"] or {}
    retr, ext = fib.get("retracement", {}), fib.get("extension", {})
    ich, bb = groups["ichimoku"], groups["bollinger_bands"]
    named = {"ma20": groups["ma20"]["value"], "kijun_sen": ich["kijun_sen"], "fib_0.382": retr.get("0.382"),
             "fib_0.5": retr.get("0.5"), "fib_0.618": retr.get("0.618")}
    entry_inputs = {k: v for k, v in named.items() if v is not None and close * (1 - ENTRY_MAX_BELOW) <= v <= close}
    notes = []
    entry = None
    if entry_inputs:
        entry = {"low": _num(min(entry_inputs.values())), "high": _num(close), "inputs": entry_inputs}
    else:
        notes.append(f"Không có MA20, Kijun hay Fibonacci 0,382-0,618 nằm trong {ENTRY_MAX_BELOW:.0%} dưới giá đóng cửa: chưa có vùng mua theo quy tắc.")
    above = {"resistance": levels["resistance"][0]["price"] if levels.get("resistance") else None,
             "bb_upper": bb["upper"], "swing_high": fib.get("swing_high")}
    above = {k: v for k, v in above.items() if v is not None and v > close}
    target_1 = min(above.values()) if above else None
    t1_source = min(above, key=above.get) if above else None
    # After a rise the upside projections are the extensions; after a fall they sit below the low,
    # so the upside steps are the retracements back toward the old high.
    if fib.get("direction") == "up":
        ladder = [(f"fib_ext_{k}", ext.get(k)) for k in ("1.272", "1.618")]
    else:
        ladder = sorted(((f"fib_{k}", v) for k, v in retr.items() if v is not None), key=lambda item: item[1])
    target_2, t2_source = None, None
    for source, value in ladder:
        if value is not None and value > (target_1 or close):
            target_2, t2_source = value, source
            break
    stop, stop_source = None, None
    if entry:
        below = {"ma50": groups["ma50"]["value"], "cloud_bottom": ich["cloud_bottom"],
                 "fib_0.618": retr.get("0.618"), "fib_0.786": retr.get("0.786")}
        below = {k: v for k, v in below.items() if v is not None and v < entry["low"]}
        if below:
            stop_source = max(below, key=below.get)
            stop = _num(below[stop_source] * (1 - STOP_BUFFER))
        else:
            notes.append("Không có MA50, đáy mây hay Fibonacci 0,618/0,786 nằm dưới vùng mua: không đặt được cắt lỗ theo quy tắc.")
    risk_reward = None
    if entry and stop is not None and target_1 is not None:
        mid = (entry["low"] + entry["high"]) / 2
        risk, reward = mid - stop, target_1 - mid
        risk_reward = {"entry_mid": _num(mid), "risk_pct": _num(risk / mid * 100), "reward_pct": _num(reward / mid * 100),
                       "ratio": _num(reward / risk) if risk > 0 else None}
    return {"close": _num(close), "entry_zone": entry,
            "target_1": {"price": _num(target_1), "source": t1_source} if target_1 is not None else None,
            "target_2": {"price": _num(target_2), "source": t2_source} if target_2 is not None else None,
            "stop_loss": {"price": stop, "source": stop_source, "buffer_pct": STOP_BUFFER * 100} if stop is not None else None,
            "risk_reward": risk_reward, "notes": notes,
            "rules": ("Vùng mua: từ mức thấp nhất trong MA20, Kijun, Fib 0,382/0,5/0,618 nằm trong 8% dưới giá đóng cửa, đến giá đóng cửa. "
                      "Mục tiêu 1: mức gần nhất trên giá trong kháng cự đầu tiên, dải Bollinger trên, đỉnh sóng. "
                      "Mục tiêu 2: mức đầu tiên cao hơn mục tiêu 1 trong Fib mở rộng 1,272, 1,618 (sóng tăng) hoặc các mốc thoái lui phía trên (sóng giảm). "
                      "Cắt lỗ: mức cao nhất trong MA50, đáy mây, Fib 0,618/0,786 nằm dưới vùng mua, trừ 1%. "
                      "R:R tính từ giữa vùng mua đến mục tiêu 1 và cắt lỗ."),
            "disclaimer": "Mức giá tính theo quy tắc cố định trên giá đã điều chỉnh, không làm tròn theo bước giá; không phải tư vấn đầu tư cá nhân."}


# ---------------------------------------------------------------- analyze
def _build(symbol: str, frame: pd.DataFrame, index_close: pd.Series | None, chart: bool) -> dict[str, Any]:
    """Everything after data loading, so offline tests can run it on synthetic frames."""
    df = frame[COLUMNS]
    warnings = list(frame.attrs.get("warnings", []))
    ind = compute_indicators(df)
    last = df.iloc[-1]
    warnings += _staleness(df.index[-1], core.now_vn())
    values, notes = _indicator_values(df, ind)
    warnings += notes
    average_value, notes = _liquidity(frame)
    warnings += notes
    values["avg_traded_value_20_vnd"] = _num(average_value, 0)
    changes = _price_changes(df["close"])
    missing = [label for label, row in changes.items() if row["pct"] is None]
    if missing:
        warnings.append(f"Không đủ dữ liệu để tính thay đổi giá {', '.join(missing)}.")
    if index_close is None:
        warnings.append(f"Không lấy được dữ liệu {BENCHMARK}: relative_strength trả null.")
    levels = find_levels(df, ind)
    for side, label in (("support", "hỗ trợ"), ("resistance", "kháng cự")):
        if not levels[side]:
            warnings.append(f"Không có ngưỡng {label} xác nhận được trong dữ liệu đã tải; "
                            f"pivot chỉ được tính sau {PIVOT_K} phiên nên đỉnh/đáy mới nhất có thể chưa xuất hiện.")
    events = detect_signals(df, ind)
    cutoff = len(df) - RECENT_SESSIONS
    recent = [{k: v for k, v in e.items() if k != "bar"} for e in reversed(events) if e["bar"] >= cutoff]
    history_note = ("Mẫu nhỏ không phải bằng chứng và tần suất trong quá khứ không dự báo tương lai; "
                    "các cửa sổ lợi suất có thể chồng lấn nên mẫu không độc lập.")
    warnings.append(history_note)
    groups = indicator_groups(df, ind)
    plan = trade_levels(float(last["close"]), groups, levels)
    chart_path = None
    if chart:
        try:
            chart_path = render_chart(symbol, df, ind, levels)
        except (OSError, ValueError, RuntimeError) as exc:
            warnings.append(f"Không vẽ được biểu đồ: {exc}")
    return {
        "symbol": symbol, "as_of": _date(df.index[-1]), "generated_at": core.now_vn().isoformat(timespec="seconds"),
        "source": frame.attrs.get("source"), "warnings": warnings,
        "price_basis": frame.attrs.get("price_basis"),
        "last": {"date": _date(df.index[-1]), "open": _num(last["open"]), "high": _num(last["high"]), "low": _num(last["low"]),
                 "close": _num(last["close"]), "volume": _num(last["volume"], 0)},
        "change": changes, "indicators": values, "trend": _trend(df, ind), "levels": levels, "signals": recent,
        "signals_window_sessions": RECENT_SESSIONS,
        "relative_strength": _relative_strength(df["close"], index_close),
        "volatility": _volatility(df["close"]),
        "signal_history": {"sessions_loaded": len(df), "warning": history_note,
                           "types": signal_statistics(events, df["close"])},
        "chart": chart_path,
        **groups,
        "trade_levels": plan,
    }


def analyze(symbol: str, days: int = 500, chart: bool = True) -> dict[str, Any]:
    symbol = core.validate_symbol(symbol)
    frame = _load(symbol, days)
    basis, notes, floor = _crosscheck(symbol, frame)
    frame.attrs["warnings"] = frame.attrs["warnings"] + notes + _gap_warnings(frame, floor)
    frame.attrs["price_basis"] = basis
    try:
        index_close = _load(BENCHMARK, days, kind="index")["close"]
    except core.SourceError:
        index_close = None
    return _build(symbol, frame, index_close, chart)
