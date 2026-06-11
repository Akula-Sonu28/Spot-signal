"""Setup H: Gap hold / gap fade."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.setups.common import (
    flags,
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_gap_bar(
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
    prior = state.prior_close
    if prior is None or prior <= 0:
        return

    gap_pct = (bar.open - prior) / prior * 100.0
    if abs(gap_pct) < research.gap_min_pct:
        return

    if bar.index == f.get("first_entry_bar", -1):
        f["open_price"] = bar.open
        f["morning_high"] = bar.high
        f["morning_low"] = bar.low
        f["gap_pct"] = gap_pct
        f["gap_up"] = gap_pct > 0
        f["gap_down"] = gap_pct < 0

    if f.get("morning_high") is not None:
        f["morning_high"] = max(f["morning_high"], bar.high)
        f["morning_low"] = min(f["morning_low"], bar.low)

    gap_level = prior
    gap_up = f.get("gap_up", False)
    gap_down = f.get("gap_down", False)

    # Gap hold CE: gap up, pullback doesn't fill gap, above VWAP, break morning high
    if (
        gap_up
        and bar.vwap is not None
        and bar.low > gap_level
        and bar.close > bar.vwap
        and bar.close > f.get("morning_high", bar.high)
    ):
        try_enter(
            state, bar, PositionSide.CE, f.get("morning_low", bar.low) - 1,
            "GAP_HOLD_CE", "H", cfg, research, logger, slippage, skipped,
        )

    # Gap fade PE: gap up, fails, loses VWAP, breaks opening low
    if (
        gap_up
        and bar.vwap is not None
        and bar.close < bar.vwap
        and bar.close < f.get("open_price", bar.open)
    ):
        try_enter(
            state, bar, PositionSide.PE, bar.high + 1,
            "GAP_FADE_PE", "H", cfg, research, logger, slippage, skipped,
        )

    # Gap down mirror
    if (
        gap_down
        and bar.vwap is not None
        and bar.high < gap_level
        and bar.close < bar.vwap
        and bar.close < f.get("morning_low", bar.low)
    ):
        try_enter(
            state, bar, PositionSide.PE, f.get("morning_high", bar.high) + 1,
            "GAP_FADE_DOWN_PE", "H", cfg, research, logger, slippage, skipped,
        )

    if (
        gap_down
        and bar.vwap is not None
        and bar.close > bar.vwap
        and bar.close > f.get("open_price", bar.open)
    ):
        try_enter(
            state, bar, PositionSide.CE, bar.low - 1,
            "GAP_HOLD_DOWN_CE", "H", cfg, research, logger, slippage, skipped,
        )
