"""v3.9 combined session bar processor — day router + v3.8 + J+."""

from __future__ import annotations

from bot.config import CombinedStrategyConfig, load_combined_config
from bot.day_router import DayMode, ensure_day_mode
from bot.logger import ReplayLogger
from bot.premium_decay_diag import log_position_snapshot
from bot.signal_enrich import enrich_target_wick_audit
from bot.state import Position, PositionSide, ReplayState
from bot.strategy import (
    BarContext,
    StrategyConfig,
    _check_exit_on_bar,
    _handle_square_off,
    _process_v38_entries,
    update_or,
)
from bot.strategy_j import process_bar_j_entries, update_trap_flags

EXIT_TYPES = frozenset({"SL_CE", "SL_PE", "TARGET_CE", "TARGET_PE", "SQUARE_OFF"})


def _spot_pnl(side: PositionSide, entry: float, exit_price: float) -> float:
    if side == PositionSide.CE:
        return exit_price - entry
    return entry - exit_price


def _finalize_position_exit(
    day,
    side: PositionSide,
    entry_price: float | None,
    active_strategy: str | None,
    logger: ReplayLogger,
) -> None:
    """Record J+ loss on any losing exit; clear active_strategy on all exits."""
    if day is None or not logger.events:
        return
    last = logger.events[-1]
    if last.event_type not in EXIT_TYPES:
        return
    if active_strategy == "j_plus" and entry_price is not None and side != PositionSide.FLAT:
        pnl = _spot_pnl(side, float(entry_price), float(last.price))
        if pnl < 0:
            day.losses_today += 1
    day.active_strategy = None


def process_session_bar(
    state: ReplayState,
    bar: BarContext,
    logger: ReplayLogger,
    combined_cfg: CombinedStrategyConfig | None = None,
) -> None:
    """Production entry point: route by OR width, shared exits."""
    combined = combined_cfg or load_combined_config()
    state.bar_index = bar.index
    day = state.day
    if day is None:
        raise RuntimeError("DayState must be initialized before process_session_bar")

    cfg: StrategyConfig = combined.strategy
    update_or(day, bar, cfg)
    mode = ensure_day_mode(day, cfg)

    if mode == DayMode.J_PLUS and bar.is_entry_window and day.or_defined:
        update_trap_flags(day, bar)

    position = state.position
    if position.side != PositionSide.FLAT:
        exit_side = position.side
        exit_entry = position.entry_price
        active_strat = day.active_strategy
        before = len(logger.events)
        if _check_exit_on_bar(position, bar, logger, cfg):
            if len(logger.events) > before:
                last = logger.events[-1]
                if last.event_type in ("TARGET_CE", "TARGET_PE"):
                    enrich_target_wick_audit(last, bar)
                _finalize_position_exit(day, exit_side, exit_entry, active_strat, logger)
            return
        # Phase 1 diagnostic: per-bar chop/decay snapshot (no strategy impact)
        snap = log_position_snapshot(position, bar, logger, cfg, chop_streak=position.chop_streak)
        position.chop_streak = int(snap.extra.get("chop_streak", 0))
        
        # Optional CHOP_STOP_EXIT replay log (Phase 2) - diagnostic only
        if snap.extra and snap.extra.get("chop_stop_exit"):
            logger.log_diagnostic(
                "CHOP_STOP_EXIT",
                bar,
                f"20min velocity chop stop triggered - advisory exit at LTP ₹{position.option_ltp or 0:.2f}",
                extra={
                    "entry_bar_index": position.entry_bar_index,
                    "bars_in_trade": bar.index - (position.entry_bar_index or 0),
                    "option_ltp": position.option_ltp,
                    "premium_velocity_min": getattr(position, 'premium_velocity_min', None),
                }
            )

    if position.side != PositionSide.FLAT:
        exit_side = position.side
        exit_entry = position.entry_price
        active_strat = day.active_strategy
        if _handle_square_off(position, bar, logger):
            _finalize_position_exit(day, exit_side, exit_entry, active_strat, logger)
            return

    if not bar.is_entry_window or mode is None:
        return

    if mode == DayMode.V38:
        _process_v38_entries(state, bar, logger, cfg)
        return

    if mode == DayMode.J_PLUS and combined.enable_j_plus:
        process_bar_j_entries(state, bar, logger, cfg, combined.j_trap)
        return

    # SKIP or J+ disabled — no entries
