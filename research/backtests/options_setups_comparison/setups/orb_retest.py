"""Setup C: ORB retest continuation."""

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
    adx_ok,
    flags,
    or_filters_ok,
    standard_preamble,
    try_enter,
    vwap_long_ok,
    vwap_short_ok,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_orb_retest_bar(
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
    if not or_filters_ok(day, bar, cfg) or not adx_ok(bar, cfg):
        return

    f = flags(state)
    prox = research.retest_proximity_pts
    adx_rising = bar.adx is not None and f.get("prev_adx") is not None and bar.adx >= f["prev_adx"]

    # Pending trigger from prior retest candle
    if state.pending is not None:
        pend = state.pending
        if pend.side == PositionSide.CE and bar.high >= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.CE, pend.structure_stop,
                pend.reason, "C", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        elif pend.side == PositionSide.PE and bar.low <= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.PE, pend.structure_stop,
                pend.reason, "C", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        else:
            state.pending = None

    or_high = day.or_high or 0.0
    or_low = day.or_low or 0.0

    # Long: initial break above OR high
    if bar.close > or_high and vwap_long_ok(bar, cfg) and not f.get("long_break_done"):
        f["long_break_done"] = True
        f["long_retest_watch"] = True

    if f.get("long_retest_watch") and state.position.side == PositionSide.FLAT and not state.pending:
        near_or = abs(bar.low - or_high) <= prox or (bar.low <= or_high <= bar.high)
        holds = bar.close >= or_high
        if near_or and holds and adx_rising:
            state.pending = PendingEntry(
                side=PositionSide.CE,
                trigger_price=bar.high,
                structure_stop=min(bar.low, or_high) - 1,
                reason="ORB_RETEST_CE",
                set_bar_index=bar.index,
            )
            f["long_retest_watch"] = False

    if bar.close < or_low and vwap_short_ok(bar, cfg) and not f.get("short_break_done"):
        f["short_break_done"] = True
        f["short_retest_watch"] = True

    if f.get("short_retest_watch") and state.position.side == PositionSide.FLAT and not state.pending:
        near_or = abs(bar.high - or_low) <= prox or (bar.low <= or_low <= bar.high)
        holds = bar.close <= or_low
        if near_or and holds and adx_rising:
            state.pending = PendingEntry(
                side=PositionSide.PE,
                trigger_price=bar.low,
                structure_stop=max(bar.high, or_low) + 1,
                reason="ORB_RETEST_PE",
                set_bar_index=bar.index,
            )
            f["short_retest_watch"] = False

    f["prev_adx"] = bar.adx
