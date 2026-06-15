"""Early OR-break watch alerts on the forming 5m bar (before bar-close confirmation)."""

from __future__ import annotations

from datetime import datetime, timedelta

from bot.config import StrategyConfig
from bot.day_router import DayMode, resolve_day_mode
from bot.indicators import or_width
from bot.state import DayState, Position, PositionSide


def bar_close_time(bar_open: datetime, *, bar_time_is_open: bool = True) -> datetime:
    if bar_time_is_open:
        return bar_open + timedelta(minutes=5)
    return bar_open


def early_watch_key(session_date: str, watch_type: str, bar_open: datetime) -> str:
    return f"{session_date}:{watch_type}:{bar_open.isoformat()}"


def detect_early_or_watch(
    day: DayState,
    position: Position,
    spot: float,
    vwap: float | None,
    adx: float | None,
    cfg: StrategyConfig,
) -> str | None:
    """
    Return WATCH_CE or WATCH_PE when the forming bar shows a pending v3.8 OR break.

    Uses the same filters as entry (OR width, ADX, VWAP) but does not require a
    closed candle — intended as a heads-up only, not an entry signal.
    """
    if not day.or_defined or day.or_high is None or day.or_low is None:
        return None
    if resolve_day_mode(day, cfg) != DayMode.V38:
        return None
    if position.side != PositionSide.FLAT:
        return None

    width = or_width(day.or_high, day.or_low)
    if width is None or not (cfg.min_or_range <= width <= cfg.max_or_range):
        return None

    adx_ok = adx is not None and adx >= cfg.adx_min
    if not adx_ok:
        return None

    vwap_long_ok = not cfg.use_vwap_filter or (vwap is not None and spot > vwap)
    vwap_short_ok = not cfg.use_vwap_filter or (vwap is not None and spot < vwap)

    long_break = spot > day.or_high
    short_break = spot < day.or_low

    if (
        long_break
        and vwap_long_ok
        and not day.fired_long_today
        and day.trades_today < cfg.max_trades_per_day
    ):
        return "WATCH_CE"

    if (
        short_break
        and vwap_short_ok
        and not day.fired_short_today
        and day.trades_today < cfg.max_trades_per_day
    ):
        return "WATCH_PE"

    return None
