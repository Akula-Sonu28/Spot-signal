"""Shared research risk engine (does not modify production bot/state.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot.config import StrategyConfig
from bot.indicators import or_width
from bot.state import DayState, Position, PositionSide, reset_position
from bot.strategy import BarContext, _calc_stops, _check_exit_on_bar

from research.backtests.options_setups_comparison.config import ResearchConfig


@dataclass
class ResearchDayState(DayState):
    """Extended day state for research setups."""

    losses_today: int = 0
    ce_trades_today: int = 0
    pe_trades_today: int = 0
    wins_today: int = 0
    setup_flags: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingEntry:
    side: PositionSide
    trigger_price: float
    structure_stop: float
    reason: str
    set_bar_index: int


@dataclass
class ResearchState:
    position: Position = field(default_factory=Position)
    day: ResearchDayState | None = None
    bar_index: int = -1
    pending: PendingEntry | None = None
    prior_close: float | None = None


@dataclass
class SkippedTrade:
    session_date: str
    bar_index: int
    setup_id: str
    side: str
    reason: str


class SkippedLogger:
    def __init__(self) -> None:
        self.entries: list[SkippedTrade] = []

    def log(self, session_date: str, bar_index: int, setup_id: str, side: str, reason: str) -> None:
        self.entries.append(
            SkippedTrade(
                session_date=session_date,
                bar_index=bar_index,
                setup_id=setup_id,
                side=side,
                reason=reason,
            )
        )


def make_research_day(session_date: str) -> ResearchDayState:
    return ResearchDayState(session_date=session_date)


def can_enter_ce(day: ResearchDayState, cfg: StrategyConfig, research: ResearchConfig) -> str | None:
    """Return skip reason if CE entry blocked, else None."""
    if day.trades_today >= cfg.max_trades_per_day:
        return "MAX_TRADES_DAY"
    if day.ce_trades_today >= 1:
        return "MAX_CE_DAY"
    if day.fired_long_today:
        return "DUPLICATE_CE"
    if day.losses_today >= research.max_losses_per_day:
        return "MAX_LOSSES_DAY"
    return None


def can_enter_pe(day: ResearchDayState, cfg: StrategyConfig, research: ResearchConfig) -> str | None:
    if day.trades_today >= cfg.max_trades_per_day:
        return "MAX_TRADES_DAY"
    if day.pe_trades_today >= 1:
        return "MAX_PE_DAY"
    if day.fired_short_today:
        return "DUPLICATE_PE"
    if day.losses_today >= research.max_losses_per_day:
        return "MAX_LOSSES_DAY"
    return None


def or_filters_ok(day: ResearchDayState, bar: BarContext, cfg: StrategyConfig) -> bool:
    width = or_width(day.or_high, day.or_low)
    if width is None:
        return False
    return cfg.min_or_range <= width <= cfg.max_or_range


def capped_stops(
    side: PositionSide,
    entry: float,
    structure_stop: float,
    atr: float,
    or_high: float,
    or_low: float,
    cfg: StrategyConfig,
) -> tuple[float, float]:
    """Structure stop capped at max 1.2x ATR distance; target at 1.8R."""
    atr_stop, atr_target = _calc_stops(side, entry, atr, or_high, or_low, cfg)
    if side == PositionSide.CE:
        max_dist = atr * cfg.atr_sl_mult
        stop = max(structure_stop, entry - max_dist)
        risk = entry - stop
        if risk <= 0:
            return stop, atr_target
        target = entry + risk * cfg.rr_ratio
        return stop, target
    max_dist = atr * cfg.atr_sl_mult
    stop = min(structure_stop, entry + max_dist)
    risk = stop - entry
    if risk <= 0:
        return stop, atr_target
    target = entry - risk * cfg.rr_ratio
    return stop, target


def open_position(
    state: ResearchState,
    side: PositionSide,
    entry: float,
    stop: float,
    target: float,
    bar: BarContext,
) -> None:
    pos = state.position
    pos.side = side
    pos.entry_price = entry
    pos.stop = stop
    pos.target = target
    pos.entry_time = bar.timestamp
    pos.entry_bar_index = bar.index


def record_entry(day: ResearchDayState, side: PositionSide, bar_index: int) -> None:
    day.trades_today += 1
    day.last_entry_bar_index = bar_index
    if side == PositionSide.CE:
        day.fired_long_today = True
        day.ce_trades_today += 1
    else:
        day.fired_short_today = True
        day.pe_trades_today += 1


def record_exit_pnl(day: ResearchDayState, side: PositionSide, entry: float, exit_price: float) -> None:
    pnl = exit_price - entry if side == PositionSide.CE else entry - exit_price
    if pnl < 0:
        day.losses_today += 1
    elif pnl > 0:
        day.wins_today += 1


def check_exit_and_log(
    state: ResearchState,
    bar: BarContext,
    logger: Any,
    slippage: Any,
) -> bool:
    """Check exit with optional slippage on fill prices in logged events."""
    pos = state.position
    if pos.side == PositionSide.FLAT:
        return False

    from bot.logger import ReplayLogger

    entry_side = pos.side
    entry_price = float(pos.entry_price or 0)
    stop = pos.stop
    target = pos.target

    temp = ReplayLogger()
    pos_copy_side = pos.side
    pos_copy_entry = pos.entry_price
    pos_copy_stop = pos.stop
    pos_copy_target = pos.target

    snap = Position(
        side=pos_copy_side,
        entry_price=pos_copy_entry,
        stop=pos_copy_stop,
        target=pos_copy_target,
    )
    exited = _check_exit_on_bar(snap, bar, temp)

    if not exited:
        if bar.is_square_off:
            side = entry_side.value
            direction = "LONG_EXIT" if entry_side == PositionSide.CE else "SHORT_EXIT"
            exit_price = slippage.adjust_exit(entry_side, bar.close)
            record_exit_pnl(state.day, entry_side, entry_price, exit_price)
            logger.log(
                timestamp=bar.timestamp,
                event_type="SQUARE_OFF",
                direction=direction,
                side=side,
                price=exit_price,
                stop=stop,
                target=target,
                reason="EOD_SQUARE_OFF",
                bar_index=bar.index,
                entry=entry_price,
            )
            reset_position(pos)
            return True
        return False

    event = temp.events[-1]
    exit_price = slippage.adjust_exit(entry_side, event.price)
    record_exit_pnl(state.day, entry_side, entry_price, exit_price)
    logger.log(
        timestamp=bar.timestamp,
        event_type=event.event_type,
        direction=event.direction,
        side=event.side,
        price=exit_price,
        stop=event.stop,
        target=event.target,
        reason=event.reason,
        bar_index=bar.index,
        entry=entry_price,
    )
    reset_position(pos)
    return True
