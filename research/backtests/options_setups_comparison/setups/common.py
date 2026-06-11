"""Shared helpers for research setup processors."""

from __future__ import annotations

from typing import Any

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext, update_or

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import (
    ResearchDayState,
    ResearchState,
    SkippedLogger,
    can_enter_ce,
    can_enter_pe,
    capped_stops,
    check_exit_and_log,
    open_position,
    or_filters_ok,
    record_entry,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def standard_preamble(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
) -> bool:
    """Update OR, handle exits. Returns True if bar processing should stop."""
    state.bar_index = bar.index
    day = state.day
    if day is None:
        raise RuntimeError("DayState not initialized")

    update_or(day, bar, cfg)
    if state.position.side != PositionSide.FLAT:
        if check_exit_and_log(state, bar, logger, slippage):
            return True
    return state.position.side != PositionSide.FLAT


def try_enter(
    state: ResearchState,
    bar: BarContext,
    side: PositionSide,
    structure_stop: float,
    reason: str,
    setup_id: str,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    entry_price: float | None = None,
) -> bool:
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return False
    if state.position.side != PositionSide.FLAT:
        return False

    block = can_enter_ce(day, cfg, research) if side == PositionSide.CE else can_enter_pe(day, cfg, research)
    if block:
        skipped.log(day.session_date, bar.index, setup_id, side.value, block)
        return False

    if bar.atr is None or day.or_high is None or day.or_low is None:
        skipped.log(day.session_date, bar.index, setup_id, side.value, "MISSING_ATR_OR")
        return False

    raw_entry = entry_price if entry_price is not None else bar.close
    entry = slippage.adjust_entry(side, raw_entry)
    stop, target = capped_stops(side, entry, structure_stop, bar.atr, day.or_high, day.or_low, cfg)
    risk = abs(entry - stop)
    if risk <= 0:
        skipped.log(day.session_date, bar.index, setup_id, side.value, "ZERO_RISK")
        return False

    open_position(state, side, entry, stop, target, bar)
    record_entry(day, side, bar.index)
    event_type = "BUY_CE" if side == PositionSide.CE else "BUY_PE"
    direction = "LONG" if side == PositionSide.CE else "SHORT"
    logger.log(
        timestamp=bar.timestamp,
        event_type=event_type,
        direction=direction,
        side=side.value,
        price=entry,
        stop=stop,
        target=target,
        reason=reason,
        bar_index=bar.index,
        setup_id=setup_id,
        or_high=day.or_high,
        or_low=day.or_low,
        adx=bar.adx,
        vwap=bar.vwap,
        atr=bar.atr,
    )
    return True


def adx_ok(bar: BarContext, cfg: StrategyConfig) -> bool:
    return bar.adx is not None and bar.adx >= cfg.adx_min


def vwap_long_ok(bar: BarContext, cfg: StrategyConfig) -> bool:
    return not cfg.use_vwap_filter or (bar.vwap is not None and bar.close > bar.vwap)


def vwap_short_ok(bar: BarContext, cfg: StrategyConfig) -> bool:
    return not cfg.use_vwap_filter or (bar.vwap is not None and bar.close < bar.vwap)


def flags(state: ResearchState) -> dict[str, Any]:
    day = state.day
    if day is None:
        return {}
    return day.setup_flags
