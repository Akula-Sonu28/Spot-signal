"""Full intraday indicator stack for non-trade-day research (not production)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr_series, session_vwap_series
from research.backtests.options_setups_comparison.indicators_ext import ema_series, rsi_series


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def _wma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    denom = period * (period + 1) / 2
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        num = sum((p + 1) * v for p, v in enumerate(window))
        out[i] = num / denom
    return out


def hma_series(values: list[float], period: int = 21) -> list[float | None]:
    half = max(1, period // 2)
    sqrt_p = max(1, int(period**0.5))
    wma_half = _wma(values, half)
    wma_full = _wma(values, period)
    raw: list[float | None] = [None] * len(values)
    for i, (a, b) in enumerate(zip(wma_half, wma_full)):
        if a is not None and b is not None:
            raw[i] = 2 * a - b
    raw_f = [v if v is not None else 0.0 for v in raw]
    return _wma(raw_f, sqrt_p)


def macd_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line: list[float | None] = [None] * len(closes)
    for i, (f, s) in enumerate(zip(ema_fast, ema_slow)):
        if f is not None and s is not None:
            macd_line[i] = f - s
    macd_vals = [v if v is not None else 0.0 for v in macd_line]
    signal_line = ema_series(macd_vals, signal)
    hist: list[float | None] = [None] * len(closes)
    for i, (m, sig) in enumerate(zip(macd_line, signal_line)):
        if m is not None and sig is not None:
            hist[i] = m - sig
    return macd_line, signal_line, hist


def bollinger_series(
    closes: list[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    mid = _sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    width: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in window) / period
        std = var**0.5
        upper[i] = m + mult * std
        lower[i] = m - mult * std
        width[i] = (upper[i] - lower[i]) / m if m else None
    return mid, upper, lower, width


def keltner_series(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    period: int = 20,
    atr_mult: float = 1.5,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = ema_series(closes, period)
    atrs = atr_series(highs, lows, closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i, (m, a) in enumerate(zip(mid, atrs)):
        if m is not None and a is not None:
            upper[i] = m + atr_mult * a
            lower[i] = m - atr_mult * a
    return mid, upper, lower


def stochastic_series(
    highs: list[float], lows: list[float], closes: list[float], k_period: int = 14, d_period: int = 3
) -> tuple[list[float | None], list[float | None]]:
    k_raw: list[float | None] = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        if hh == ll:
            k_raw[i] = 50.0
        else:
            k_raw[i] = 100.0 * (closes[i] - ll) / (hh - ll)
    k_vals = [v if v is not None else 50.0 for v in k_raw]
    d_line = _sma(k_vals, d_period)
    return k_raw, d_line


def cmf_series(
    highs: list[float], lows: list[float], closes: list[float], volumes: list[float], period: int = 20
) -> list[float | None]:
    mfv: list[float] = []
    for h, l, c, v in zip(highs, lows, closes, volumes):
        if h == l:
            mfv.append(0.0)
        else:
            mfm = ((c - l) - (h - c)) / (h - l)
            mfv.append(mfm * v)
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        vol_sum = sum(volumes[i - period + 1 : i + 1])
        if vol_sum <= 0:
            continue
        out[i] = sum(mfv[i - period + 1 : i + 1]) / vol_sum
    return out


def cci_series(highs: list[float], lows: list[float], closes: list[float], period: int = 20) -> list[float | None]:
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = tp[i - period + 1 : i + 1]
        sma = sum(window) / period
        mad = sum(abs(x - sma) for x in window) / period
        if mad == 0:
            out[i] = 0.0
        else:
            out[i] = (tp[i] - sma) / (0.015 * mad)
    return out


def obv_series_per_session(closes: list[float], volumes: list[float], session_ids: list[str]) -> list[float]:
    out: list[float] = [0.0] * len(closes)
    obv = 0.0
    cur_sess: str | None = None
    for i, (c, v, sid) in enumerate(zip(closes, volumes, session_ids)):
        if sid != cur_sess:
            cur_sess = sid
            obv = 0.0
        if i > 0 and closes[i - 1] != c:
            if c > closes[i - 1]:
                obv += v
            elif c < closes[i - 1]:
                obv -= v
        out[i] = obv
    return out


def parabolic_sar_series(
    highs: list[float], lows: list[float], af_step: float = 0.02, af_max: float = 0.2
) -> tuple[list[float | None], list[bool | None]]:
    """SAR value and is_long (bullish when price above SAR)."""
    n = len(highs)
    sar: list[float | None] = [None] * n
    bull: list[bool | None] = [None] * n
    if n < 2:
        return sar, bull
    is_long = highs[1] + lows[1] > highs[0] + lows[0]
    ep = highs[0] if is_long else lows[0]
    sar[0] = lows[0] if is_long else highs[0]
    af = af_step
    bull[0] = is_long
    for i in range(1, n):
        prev_sar = sar[i - 1] if sar[i - 1] is not None else (lows[i - 1] if is_long else highs[i - 1])
        sar[i] = prev_sar + af * (ep - prev_sar)
        if is_long:
            sar[i] = min(sar[i], lows[i - 1], lows[i] if i > 0 else lows[i - 1])
            if lows[i] < sar[i]:
                is_long = False
                sar[i] = ep
                ep = lows[i]
                af = af_step
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
        else:
            sar[i] = max(sar[i], highs[i - 1], highs[i] if i > 0 else highs[i - 1])
            if highs[i] > sar[i]:
                is_long = True
                sar[i] = ep
                ep = highs[i]
                af = af_step
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)
        bull[i] = is_long
    return sar, bull


def session_poc_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    session_ids: list[str],
    bin_size: float = 10.0,
) -> list[float | None]:
    """Simplified VPVR: session point-of-control (max volume price bin)."""
    out: list[float | None] = [None] * len(closes)
    cur: str | None = None
    vol_by_bin: dict[int, float] = {}
    poc: float | None = None

    def _bin(price: float) -> int:
        return int(round(price / bin_size))

    for i, (h, l, c, v, sid) in enumerate(zip(highs, lows, closes, volumes, session_ids)):
        if sid != cur:
            cur = sid
            vol_by_bin = {}
            poc = None
        typical = (h + l + c) / 3.0
        b = _bin(typical)
        vol_by_bin[b] = vol_by_bin.get(b, 0.0) + v
        best_bin = max(vol_by_bin, key=lambda b: vol_by_bin[b])
        poc = float(best_bin) * bin_size
        out[i] = poc
    return out


@dataclass(frozen=True)
class IndicatorStack:
    """Precomputed per-bar indicators aligned to enriched dataframe rows."""

    vwap: list[float | None]
    avwap: list[float | None]  # session-anchored = vwap at open
    poc: list[float | None]
    ema9: list[float | None]
    ema21: list[float | None]
    ema50: list[float | None]
    hma21: list[float | None]
    macd: list[float | None]
    macd_signal: list[float | None]
    macd_hist: list[float | None]
    bb_mid: list[float | None]
    bb_upper: list[float | None]
    bb_lower: list[float | None]
    bb_width: list[float | None]
    kc_mid: list[float | None]
    kc_upper: list[float | None]
    kc_lower: list[float | None]
    rsi: list[float | None]
    stoch_k: list[float | None]
    stoch_d: list[float | None]
    atr: list[float | None]
    cmf: list[float | None]
    sar: list[float | None]
    sar_bull: list[bool | None]
    cci: list[float | None]
    obv: list[float]

    def at(self, i: int) -> dict:
        return {k: getattr(self, k)[i] for k in IndicatorStack.__dataclass_fields__}


def build_indicator_stack(df: pd.DataFrame) -> IndicatorStack:
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    closes = df["close"].astype(float).tolist()
    volumes = df["volume"].astype(float).tolist()
    session_ids = df["session_date"].astype(str).tolist()

    typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    if "vwap" in df.columns:
        vwap = [float(v) if pd.notna(v) else None for v in df["vwap"]]
    else:
        vwap = session_vwap_series(typical, volumes, session_ids)

    macd, sig, hist = macd_series(closes)
    bb_mid, bb_up, bb_lo, bb_w = bollinger_series(closes)
    kc_mid, kc_up, kc_lo = keltner_series(closes, highs, lows)
    st_k, st_d = stochastic_series(highs, lows, closes)
    sar, sar_bull = parabolic_sar_series(highs, lows)

    return IndicatorStack(
        vwap=vwap,
        avwap=vwap,
        poc=session_poc_series(highs, lows, closes, volumes, session_ids),
        ema9=ema_series(closes, 9),
        ema21=ema_series(closes, 21),
        ema50=ema_series(closes, 50),
        hma21=hma_series(closes, 21),
        macd=macd,
        macd_signal=sig,
        macd_hist=hist,
        bb_mid=bb_mid,
        bb_upper=bb_up,
        bb_lower=bb_lo,
        bb_width=bb_w,
        kc_mid=kc_mid,
        kc_upper=kc_up,
        kc_lower=kc_lo,
        rsi=rsi_series(closes, 14),
        stoch_k=st_k,
        stoch_d=st_d,
        atr=atr_series(highs, lows, closes, 14),
        cmf=cmf_series(highs, lows, closes, volumes),
        sar=sar,
        sar_bull=sar_bull,
        cci=cci_series(highs, lows, closes),
        obv=obv_series_per_session(closes, volumes, session_ids),
    )
