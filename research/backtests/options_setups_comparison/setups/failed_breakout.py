"""Setup F: Failed breakout reversal."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import (
    PendingEntry,
    ResearchState,
    SkippedLogger,
)
from research.backtests.options_setups_comparison.setups.common import (
    flags,
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_failed_breakout_bar(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
) -> None:
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return

    f = flags(state)
    or_high = day.or_high or 0.0
    or_low = day.or_low or 0.0

    if state.pending is not None:
        pend = state.pending
        if pend.side == PositionSide.PE and bar.low <= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.PE, pend.structure_stop,
                pend.reason, "F", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        elif pend.side == PositionSide.CE and bar.high >= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.CE, pend.structure_stop,
                pend.reason, "F", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        elif bar.index > pend.set_bar_index + 2:
            state.pending = None

    # Track false breakout above OR
    if bar.high > or_high and not f.get("failed_up_tracked"):
        f["false_break_bar"] = bar.index
        f["false_break_high"] = bar.high
        f["failed_up_tracked"] = True

    if f.get("false_break_bar") is not None and bar.index - f["false_break_bar"] <= 3:
        if bar.close < or_high and bar.vwap is not None and bar.close < bar.vwap:
            fail_low = bar.low
            state.pending = PendingEntry(
                side=PositionSide.PE,
                trigger_price=fail_low,
                structure_stop=bar.high + 1,
                reason="FAILED_BREAKOUT_PE",
                set_bar_index=bar.index,
            )
            f["false_break_bar"] = None

    if bar.low < or_low and not f.get("failed_down_tracked"):
        f["false_break_down_bar"] = bar.index
        f["failed_down_tracked"] = True

    if f.get("false_break_down_bar") is not None and bar.index - f["false_break_down_bar"] <= 3:
        if bar.close > or_low and bar.vwap is not None and bar.close > bar.vwap:
            fail_high = bar.high
            state.pending = PendingEntry(
                side=PositionSide.CE,
                trigger_price=fail_high,
                structure_stop=bar.low - 1,
                reason="FAILED_BREAKDOWN_CE",
                set_bar_index=bar.index,
            )
            f["false_break_down_bar"] = None
