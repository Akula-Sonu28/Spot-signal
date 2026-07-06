"""Setup J+ — wide-OR fake-break trap fade (v3.9 Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import DayState, PositionSide, ReplayState
from bot.stops import capped_stops
from bot.strategy import BarContext, _minutes_from_midnight, _session_times, new_entries_allowed


@dataclass(frozen=True)
class JTrapConfig:
    """Robustness filters for Setup J+."""

    min_trap_excess_pts: float = 0.0
    min_reclaim_pts: float = 0.0
    min_vwap_dist_pts: float = 0.0
    min_body_ratio: float = 0.0
    min_adx: float | None = None
    skip_both_trapped: bool = False
    min_minutes_after_or: int = 0
    max_trades_day: int = 2
    max_losses_day: int = 2
    one_trap_side_only: bool = False


J_TRAP_ROBUST = JTrapConfig(
    min_trap_excess_pts=15.0,
    min_reclaim_pts=8.0,
    min_vwap_dist_pts=8.0,
    min_body_ratio=0.4,
    min_adx=20.0,
    skip_both_trapped=True,
    min_minutes_after_or=30,
    max_trades_day=1,
    max_losses_day=1,
    one_trap_side_only=False,
)


def _body_ratio(bar: BarContext, bullish: bool) -> float:
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    body = bar.close - bar.open if bullish else bar.open - bar.close
    return max(0.0, body / rng)


def _minutes_after_or(bar: BarContext, cfg: StrategyConfig) -> int:
    _, or_end, _ = _session_times(cfg)
    close_min = _minutes_from_midnight(bar.timestamp)
    if cfg.bar_time_is_open:
        close_min += 5
    return close_min - or_end


def _adx_ok(bar: BarContext, cfg: StrategyConfig, jcfg: JTrapConfig) -> bool:
    floor = jcfg.min_adx if jcfg.min_adx is not None else cfg.adx_min
    return bar.adx is not None and bar.adx >= floor


def update_trap_flags(day: DayState, bar: BarContext) -> None:
    """Track OR fake-break wicks on J+ days (full entry window, before time gate)."""
    if day.or_high is None or day.or_low is None:
        return
    if bar.high > day.or_high:
        if not day.touched_above_or:
            day.touched_above_or = True
            day.trap_high = bar.high
            day.first_trap_side = "PE"
        day.trap_high = max(day.trap_high or bar.high, bar.high)
    if bar.low < day.or_low:
        if not day.touched_below_or:
            day.touched_below_or = True
            day.trap_low = bar.low
            if day.first_trap_side is None:
                day.first_trap_side = "CE"
        day.trap_low = min(day.trap_low or bar.low, bar.low)


def _open_j_position(
    state: ReplayState,
    bar: BarContext,
    logger: ReplayLogger,
    side: PositionSide,
    stop: float,
    target: float,
    reason: str,
) -> None:
    day = state.day
    position = state.position
    if day is None:
        return
    position.side = side
    position.entry_price = bar.close
    position.stop = stop
    position.target = target
    position.entry_time = bar.timestamp
    position.entry_bar_index = bar.index
    day.active_strategy = "j_plus"
    event_type = "BUY_CE" if side == PositionSide.CE else "BUY_PE"
    direction = "LONG" if side == PositionSide.CE else "SHORT"
    side_val = side.value
    logger.log(
        timestamp=bar.timestamp,
        event_type=event_type,
        direction=direction,
        side=side_val,
        price=bar.close,
        stop=stop,
        target=target,
        reason=reason,
        bar_index=bar.index,
        or_high=day.or_high,
        or_low=day.or_low,
        adx=bar.adx,
        vwap=bar.vwap,
        strategy="j_plus",
    )
    day.trades_today += 1
    day.last_entry_bar_index = bar.index
    if side == PositionSide.CE:
        day.fired_long_today = True
    else:
        day.fired_short_today = True


def process_bar_j_entries(
    state: ReplayState,
    bar: BarContext,
    logger: ReplayLogger,
    cfg: StrategyConfig,
    jcfg: JTrapConfig,
) -> None:
    """J+ trap-fade entries only (trap flags updated earlier by combined dispatcher)."""
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    if not new_entries_allowed(bar, cfg):
        return
    if state.position.side != PositionSide.FLAT:
        return
    if bar.vwap is None or day.or_high is None or day.or_low is None or bar.atr is None:
        return
    if day.trades_today >= jcfg.max_trades_day:
        return
    if jcfg.max_losses_day > 0 and day.losses_today >= jcfg.max_losses_day:
        return
    if _minutes_after_or(bar, cfg) < jcfg.min_minutes_after_or:
        return

    if jcfg.skip_both_trapped and day.touched_above_or and day.touched_below_or:
        return

    or_high = day.or_high
    or_low = day.or_low
    trap_high = day.trap_high or bar.high
    trap_low = day.trap_low or bar.low

    pe_base = (
        day.touched_above_or
        and bar.close < or_high
        and bar.close < bar.vwap
        and _adx_ok(bar, cfg, jcfg)
        and bar.close < bar.open
    )
    ce_base = (
        day.touched_below_or
        and bar.close > or_low
        and bar.close > bar.vwap
        and _adx_ok(bar, cfg, jcfg)
        and bar.close > bar.open
    )

    if jcfg.one_trap_side_only:
        first = day.first_trap_side
        if first == "PE":
            ce_base = False
        elif first == "CE":
            pe_base = False

    pe_ok = (
        pe_base
        and (trap_high - or_high) >= jcfg.min_trap_excess_pts
        and (or_high - bar.close) >= jcfg.min_reclaim_pts
        and (bar.vwap - bar.close) >= jcfg.min_vwap_dist_pts
        and _body_ratio(bar, bullish=False) >= jcfg.min_body_ratio
    )
    ce_ok = (
        ce_base
        and (or_low - trap_low) >= jcfg.min_trap_excess_pts
        and (bar.close - or_low) >= jcfg.min_reclaim_pts
        and (bar.close - bar.vwap) >= jcfg.min_vwap_dist_pts
        and _body_ratio(bar, bullish=True) >= jcfg.min_body_ratio
    )

    if pe_ok:
        structure = max(trap_high, bar.high) + cfg.sl_buffer_pts
        stop, target = capped_stops(PositionSide.PE, bar.close, structure, bar.atr, cfg)
        if stop > bar.close:
            _open_j_position(state, bar, logger, PositionSide.PE, stop, target, "OR_FAKE_BREAK_PE")
        return

    if ce_ok:
        structure = min(trap_low, bar.low) - cfg.sl_buffer_pts
        stop, target = capped_stops(PositionSide.CE, bar.close, structure, bar.atr, cfg)
        if bar.close > stop:
            _open_j_position(state, bar, logger, PositionSide.CE, stop, target, "OR_FAKE_BREAK_CE")
