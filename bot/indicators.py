"""Technical indicators aligned with TradingView-style Wilder smoothing."""

from __future__ import annotations

import math
from typing import Sequence


def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def wilder_rma(values: Sequence[float], length: int) -> list[float | None]:
    """Wilder's RMA (matches Pine ta.rma / ta.atr / ta.dmi smoothing)."""
    if length <= 0:
        raise ValueError("length must be positive")

    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out

    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def atr_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    length: int,
) -> list[float | None]:
    trs: list[float] = []
    for i in range(len(closes)):
        prev_close = closes[i - 1] if i > 0 else None
        trs.append(true_range(highs[i], lows[i], prev_close))
    return wilder_rma(trs, length)


def adx_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    length: int,
) -> list[float | None]:
    """ADX using Wilder smoothing (Pine ta.dmi with same di/adx length)."""
    n = len(closes)
    if n == 0:
        return []

    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    trs: list[float] = [0.0] * n

    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0
        trs[i] = true_range(highs[i], lows[i], closes[i - 1])

    tr_rma = wilder_rma(trs, length)
    plus_rma = wilder_rma(plus_dm, length)
    minus_rma = wilder_rma(minus_dm, length)

    dx: list[float | None] = [None] * n
    for i in range(n):
        if tr_rma[i] is None or tr_rma[i] == 0:
            continue
        plus_di = 100.0 * (plus_rma[i] or 0.0) / tr_rma[i]
        minus_di = 100.0 * (minus_rma[i] or 0.0) / tr_rma[i]
        denom = plus_di + minus_di
        if denom == 0:
            dx[i] = 0.0
        else:
            dx[i] = 100.0 * abs(plus_di - minus_di) / denom

    dx_clean = [0.0 if v is None else v for v in dx]
    adx = wilder_rma(dx_clean, length)
    return adx


def session_vwap_series(
    typical_prices: Sequence[float],
    volumes: Sequence[float],
    session_ids: Sequence[str],
) -> list[float | None]:
    """Intraday VWAP reset per session_id (trading day)."""
    out: list[float | None] = [None] * len(typical_prices)
    cum_tpv = 0.0
    cum_vol = 0.0
    current_session: str | None = None

    for i, (tp, vol, sid) in enumerate(zip(typical_prices, volumes, session_ids)):
        if sid != current_session:
            current_session = sid
            cum_tpv = 0.0
            cum_vol = 0.0

        if vol <= 0:
            out[i] = out[i - 1] if i > 0 else None
            continue

        cum_tpv += tp * vol
        cum_vol += vol
        out[i] = cum_tpv / cum_vol if cum_vol > 0 else None

    return out


def opening_range(
    highs: Sequence[float],
    lows: Sequence[float],
    in_or_flags: Sequence[bool],
) -> tuple[float | None, float | None, bool]:
    """Compute OR high/low from bars flagged as inside opening range."""
    or_high: float | None = None
    or_low: float | None = None
    for h, l, flag in zip(highs, lows, in_or_flags):
        if not flag:
            continue
        or_high = h if or_high is None else max(or_high, h)
        or_low = l if or_low is None else min(or_low, l)

    defined = or_high is not None and or_low is not None
    return or_high, or_low, defined


def or_width(or_high: float | None, or_low: float | None) -> float | None:
    if or_high is None or or_low is None:
        return None
    return or_high - or_low
