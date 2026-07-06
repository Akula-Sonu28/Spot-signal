"""NIFTY Spot Signal Engine — v3.8 OR breakout entries, OR_RANGE + close-only SL exits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.state import DayState, Position, PositionSide, ReplayState, reset_position

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class BarContext:
    """Single confirmed 5m bar with precomputed indicators."""

    index: int
    timestamp: datetime  # bar close time in IST
    session_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    atr: float | None
    adx: float | None
    in_or: bool
    is_after_or: bool
    is_entry_window: bool
    is_square_off: bool


def _minutes_from_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _bar_close_minutes(bar_min: int, cfg: StrategyConfig) -> int:
    return bar_min + 5 if cfg.bar_time_is_open else bar_min


def new_entries_allowed(bar: BarContext, cfg: StrategyConfig = DEFAULT_CONFIG) -> bool:
    """True when bar close is on or before no_new_entries_after (v3.10 afternoon gate)."""
    cutoff = cfg.no_new_entries_after
    if not cutoff or not str(cutoff).strip():
        return True
    parts = str(cutoff).strip().split(":")
    if len(parts) != 2:
        return True
    gate_min = int(parts[0]) * 60 + int(parts[1])
    close_min = _bar_close_minutes(_minutes_from_midnight(bar.timestamp), cfg)
    return close_min <= gate_min


def _session_times(cfg: StrategyConfig) -> tuple[int, int, int]:
    market_open = cfg.market_open_h * 60 + cfg.market_open_m
    or_end = market_open + cfg.or_minutes
    square_off = cfg.square_off_h * 60 + cfg.square_off_m
    return market_open, or_end, square_off


def build_bar_context(
    index: int,
    timestamp: datetime,
    session_date: str,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: float,
    vwap: float | None,
    atr: float | None,
    adx: float | None,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    tz: ZoneInfo | None = None,
) -> BarContext:
    """Classify bar relative to OR / entry / square-off windows."""
    tz = tz or ZoneInfo(cfg.timezone)
    if timestamp.tzinfo is None:
        ts = timestamp.replace(tzinfo=tz)
    else:
        ts = timestamp.astimezone(tz)

    market_open, or_end, square_off = _session_times(cfg)
    bar_min = _minutes_from_midnight(ts)
    # Pine uses time_close; Upstox timestamps are bar open → add 5m for parity
    close_min = bar_min + 5 if cfg.bar_time_is_open else bar_min
    in_or_by_time = market_open <= close_min < or_end

    return BarContext(
        index=index,
        timestamp=ts,
        session_date=session_date,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=vol,
        vwap=vwap,
        atr=atr,
        adx=adx,
        in_or=in_or_by_time,
        is_after_or=close_min >= or_end,
        is_entry_window=close_min >= or_end and close_min < square_off,
        is_square_off=close_min >= square_off,
    )


def update_or(day: DayState, bar: BarContext, cfg: StrategyConfig = DEFAULT_CONFIG) -> None:
    """Accumulate OR from all bars in opening window (matches Pine isInOR)."""
    if bar.in_or:
        day.or_bars_seen += 1
        day.or_high = bar.high if day.or_high is None else max(day.or_high, bar.high)
        day.or_low = bar.low if day.or_low is None else min(day.or_low, bar.low)

    if bar.is_after_or and day.or_high is not None and day.or_low is not None:
        day.or_defined = True


def _calc_stop(
    side: PositionSide,
    entry: float,
    atr: float,
    or_high: float,
    or_low: float,
    cfg: StrategyConfig,
) -> float | None:
    if not cfg.use_stop_loss:
        return None

    atr_stop_dist = atr * cfg.atr_sl_mult
    buffer = cfg.sl_buffer_pts
    if side == PositionSide.CE:
        long_stop_atr = entry - atr_stop_dist - buffer
        long_stop_or = or_low - buffer
        if cfg.sl_mode == "ATR":
            return long_stop_atr
        if cfg.sl_mode == "OR_RANGE":
            return long_stop_or
        if cfg.sl_mode == "WIDER":
            return min(long_stop_atr, long_stop_or)
        return max(long_stop_atr, long_stop_or)

    short_stop_atr = entry + atr_stop_dist + buffer
    short_stop_or = or_high + buffer
    if cfg.sl_mode == "ATR":
        return short_stop_atr
    if cfg.sl_mode == "OR_RANGE":
        return short_stop_or
    if cfg.sl_mode == "WIDER":
        return max(short_stop_atr, short_stop_or)
    return min(short_stop_atr, short_stop_or)


def _calc_target(side: PositionSide, entry: float, atr: float, cfg: StrategyConfig) -> float:
    reward = atr * cfg.atr_target_mult * cfg.rr_ratio
    if side == PositionSide.CE:
        return entry + reward
    return entry - reward


def _calc_stops(
    side: PositionSide,
    entry: float,
    atr: float,
    or_high: float,
    or_low: float,
    cfg: StrategyConfig,
) -> tuple[float | None, float]:
    stop = _calc_stop(side, entry, atr, or_high, or_low, cfg)
    target = _calc_target(side, entry, atr, cfg)
    return stop, target


def _check_exit_on_bar(
    position: Position,
    bar: BarContext,
    logger: ReplayLogger,
    cfg: StrategyConfig = DEFAULT_CONFIG,
) -> bool:
    """Intrabar SL/target check. SL takes priority if both touched (conservative)."""
    if position.side == PositionSide.FLAT:
        return False

    stop = position.stop
    target = position.target
    entry = position.entry_price
    if target is None or entry is None:
        return False

    bars_in_trade = (
        bar.index - position.entry_bar_index
        if position.entry_bar_index is not None
        else cfg.sl_delay_bars
    )
    sl_active = bars_in_trade >= cfg.sl_delay_bars

    exited = False
    if position.side == PositionSide.CE:
        sl_hit = (
            sl_active
            and stop is not None
            and (bar.close <= stop if cfg.close_only_sl else bar.low <= stop)
        )
        tgt_hit = not sl_hit and bar.high >= target
        if sl_hit:
            logger.log(
                timestamp=bar.timestamp,
                event_type="SL_CE",
                direction="LONG_EXIT",
                side="CE",
                price=stop,
                stop=stop,
                target=target,
                reason="STOP_HIT",
                bar_index=bar.index,
                entry=entry,
            )
            exited = True
        elif tgt_hit:
            logger.log(
                timestamp=bar.timestamp,
                event_type="TARGET_CE",
                direction="LONG_EXIT",
                side="CE",
                price=target,
                stop=stop,
                target=target,
                reason="TARGET_HIT",
                bar_index=bar.index,
                entry=entry,
            )
            exited = True
    else:
        sl_hit = (
            sl_active
            and stop is not None
            and (bar.close >= stop if cfg.close_only_sl else bar.high >= stop)
        )
        tgt_hit = not sl_hit and bar.low <= target
        if sl_hit:
            logger.log(
                timestamp=bar.timestamp,
                event_type="SL_PE",
                direction="SHORT_EXIT",
                side="PE",
                price=stop,
                stop=stop,
                target=target,
                reason="STOP_HIT",
                bar_index=bar.index,
                entry=entry,
            )
            exited = True
        elif tgt_hit:
            logger.log(
                timestamp=bar.timestamp,
                event_type="TARGET_PE",
                direction="SHORT_EXIT",
                side="PE",
                price=target,
                stop=stop,
                target=target,
                reason="TARGET_HIT",
                bar_index=bar.index,
                entry=entry,
            )
            exited = True

    if exited:
        reset_position(position)
    return exited


def _handle_square_off(
    position: Position,
    bar: BarContext,
    logger: ReplayLogger,
) -> bool:
    if not bar.is_square_off or position.side == PositionSide.FLAT:
        return False
    side = position.side.value
    direction = "LONG_EXIT" if position.side == PositionSide.CE else "SHORT_EXIT"
    logger.log(
        timestamp=bar.timestamp,
        event_type="SQUARE_OFF",
        direction=direction,
        side=side,
        price=bar.close,
        stop=position.stop,
        target=position.target,
        reason="EOD_SQUARE_OFF",
        bar_index=bar.index,
        entry=position.entry_price,
    )
    reset_position(position)
    return True


def _process_v38_entries(
    state: ReplayState,
    bar: BarContext,
    logger: ReplayLogger,
    cfg: StrategyConfig,
) -> None:
    """v3.8 OR breakout entries (valid OR width only)."""
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    if not new_entries_allowed(bar, cfg):
        return

    position = state.position
    width = or_width(day.or_high, day.or_low)
    or_width_ok = width is not None and cfg.min_or_range <= width <= cfg.max_or_range
    adx_ok = bar.adx is not None and bar.adx >= cfg.adx_min
    vwap_long_ok = not cfg.use_vwap_filter or (bar.vwap is not None and bar.close > bar.vwap)
    vwap_short_ok = not cfg.use_vwap_filter or (bar.vwap is not None and bar.close < bar.vwap)

    long_break = bar.close > (day.or_high or float("inf"))
    short_break = bar.close < (day.or_low or float("-inf"))

    is_flat = position.side == PositionSide.FLAT
    no_dup = day.last_entry_bar_index is None or bar.index > day.last_entry_bar_index
    can_trade = day.trades_today < cfg.max_trades_per_day

    buy_ce = (
        long_break
        and vwap_long_ok
        and adx_ok
        and or_width_ok
        and can_trade
        and no_dup
        and not day.fired_long_today
        and is_flat
    )
    buy_pe = (
        short_break
        and vwap_short_ok
        and adx_ok
        and or_width_ok
        and can_trade
        and no_dup
        and not day.fired_short_today
        and is_flat
    )

    if buy_ce and bar.atr is not None and day.or_high is not None and day.or_low is not None:
        stop, target = _calc_stops(
            PositionSide.CE, bar.close, bar.atr, day.or_high, day.or_low, cfg
        )
        if stop is not None and bar.close - stop <= 0:
            return
        if _calc_target(PositionSide.CE, bar.close, bar.atr, cfg) <= bar.close:
            return
        position.side = PositionSide.CE
        position.entry_price = bar.close
        position.stop = stop
        position.target = target
        position.entry_time = bar.timestamp
        position.entry_bar_index = bar.index
        day.active_strategy = "v38"
        logger.log(
            timestamp=bar.timestamp,
            event_type="BUY_CE",
            direction="LONG",
            side="CE",
            price=bar.close,
            stop=stop,
            target=target,
            reason="ORB_BREAKOUT+VWAP+ADX",
            bar_index=bar.index,
            or_high=day.or_high,
            or_low=day.or_low,
            adx=bar.adx,
            vwap=bar.vwap,
            strategy="v38",
        )
        day.trades_today += 1
        day.fired_long_today = True
        day.last_entry_bar_index = bar.index
        return

    if buy_pe and bar.atr is not None and day.or_high is not None and day.or_low is not None:
        stop, target = _calc_stops(
            PositionSide.PE, bar.close, bar.atr, day.or_high, day.or_low, cfg
        )
        if stop is not None and stop - bar.close <= 0:
            return
        if _calc_target(PositionSide.PE, bar.close, bar.atr, cfg) >= bar.close:
            return
        position.side = PositionSide.PE
        position.entry_price = bar.close
        position.stop = stop
        position.target = target
        position.entry_time = bar.timestamp
        position.entry_bar_index = bar.index
        day.active_strategy = "v38"
        logger.log(
            timestamp=bar.timestamp,
            event_type="BUY_PE",
            direction="SHORT",
            side="PE",
            price=bar.close,
            stop=stop,
            target=target,
            reason="ORB_BREAKDOWN+VWAP+ADX",
            bar_index=bar.index,
            or_high=day.or_high,
            or_low=day.or_low,
            adx=bar.adx,
            vwap=bar.vwap,
            strategy="v38",
        )
        day.trades_today += 1
        day.fired_short_today = True
        day.last_entry_bar_index = bar.index


def process_bar(
    state: ReplayState,
    bar: BarContext,
    logger: ReplayLogger,
    cfg: StrategyConfig = DEFAULT_CONFIG,
) -> None:
    """Process one confirmed bar (v3.8-only legacy path)."""
    state.bar_index = bar.index
    day = state.day
    if day is None:
        raise RuntimeError("DayState must be initialized before process_bar")

    update_or(day, bar, cfg)

    position = state.position
    if position.side != PositionSide.FLAT:
        if _check_exit_on_bar(position, bar, logger, cfg):
            return

    if _handle_square_off(position, bar, logger):
        return

    _process_v38_entries(state, bar, logger, cfg)
