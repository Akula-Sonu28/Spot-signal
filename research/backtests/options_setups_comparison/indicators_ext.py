"""Research-only extended indicators (not added to bot/indicators.py)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from bot.indicators import session_vwap_series


def ema_series(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average; None until enough samples."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or not values:
        return out
    alpha = 2.0 / (period + 1)
    ema: float | None = None
    for i, v in enumerate(values):
        if ema is None:
            if i + 1 < period:
                continue
            ema = sum(values[i + 1 - period : i + 1]) / period
        else:
            ema = alpha * v + (1 - alpha) * ema
        out[i] = ema
    return out


def compression_width_ratio(
    highs: list[float],
    lows: list[float],
    atr: float | None,
    end_index: int,
    lookback: int = 6,
) -> float | None:
    """Range width of last `lookback` bars ending at end_index, divided by ATR."""
    if atr is None or atr <= 0 or end_index < lookback - 1:
        return None
    start = end_index - lookback + 1
    window_h = max(highs[start : end_index + 1])
    window_l = min(lows[start : end_index + 1])
    return (window_h - window_l) / atr


def swing_high(highs: list[float], end_index: int, lookback: int = 5) -> float | None:
    if end_index < lookback - 1:
        return None
    start = end_index - lookback + 1
    return max(highs[start : end_index + 1])


def swing_low(lows: list[float], end_index: int, lookback: int = 5) -> float | None:
    if end_index < lookback - 1:
        return None
    start = end_index - lookback + 1
    return min(lows[start : end_index + 1])


def vwap_slope(vwaps: list[float | None], end_index: int, lookback: int = 3) -> float | None:
    """Simple slope: current vwap minus vwap lookback bars ago."""
    if end_index < lookback:
        return None
    cur = vwaps[end_index]
    prev = vwaps[end_index - lookback]
    if cur is None or prev is None:
        return None
    return cur - prev


def prior_session_closes(df: pd.DataFrame, session_dates: list[str]) -> dict[str, float | None]:
    """Map session_date -> prior trading session last close."""
    closes: dict[str, float | None] = {}
    sorted_dates = sorted(set(session_dates))
    for i, sd in enumerate(sorted_dates):
        if i == 0:
            closes[sd] = None
            continue
        prev = sorted_dates[i - 1]
        prev_rows = df[df["session_date"] == prev]
        if prev_rows.empty:
            closes[sd] = None
        else:
            closes[sd] = float(prev_rows.iloc[-1]["close"])
    return closes


def is_expiry_day(session_date: str) -> bool:
    """NIFTY weekly expiry heuristic: Tuesday."""
    return date.fromisoformat(session_date).weekday() == 1


def weekday_name(session_date: str) -> str:
    return date.fromisoformat(session_date).strftime("%A")


def time_bucket(entry_time_iso: str) -> str:
    """Classify entry hour into intraday buckets."""
    from datetime import datetime

    dt = datetime.fromisoformat(entry_time_iso)
    hm = dt.hour * 60 + dt.minute
    if hm < 11 * 60:
        return "09:30-11:00"
    if hm < 13 * 60:
        return "11:00-13:00"
    return "13:00-15:15"


def validate_vwap_reset(df: pd.DataFrame, session_date: str, tolerance: float = 1.0) -> bool:
    """Recompute session VWAP and compare to cached column."""
    rows = df[df["session_date"] == session_date]
    if rows.empty or "vwap" not in rows.columns:
        return False
    highs = rows["high"].astype(float).tolist()
    lows = rows["low"].astype(float).tolist()
    closes = rows["close"].astype(float).tolist()
    volumes = rows["volume"].astype(float).tolist()
    typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    session_ids = [session_date] * len(typical)
    recomputed = session_vwap_series(typical, volumes, session_ids)
    for i, (cached, calc) in enumerate(zip(rows["vwap"].astype(float), recomputed)):
        if calc is None:
            continue
        if abs(cached - calc) > tolerance and i > 0:
            return False
    return True


def expected_weekdays(from_date: str, to_date: str) -> list[str]:
    """Weekdays in range (may include holidays without files)."""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    out: list[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out
