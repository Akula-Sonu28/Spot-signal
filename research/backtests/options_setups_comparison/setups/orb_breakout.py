"""Setup B: Raw ORB breakout with unified research risk engine."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
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


def process_orb_breakout_bar(
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

    sid = flags(state).get("setup_id", "B")
    if (
        bar.close > (day.or_high or float("inf"))
        and vwap_long_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.CE, (day.or_low or bar.close) - 1,
            "RAW_ORB_BREAKOUT+VWAP+ADX", sid, cfg, research, logger, slippage, skipped,
        )
    elif (
        bar.close < (day.or_low or float("-inf"))
        and vwap_short_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.PE, (day.or_high or bar.close) + 1,
            "RAW_ORB_BREAKDOWN+VWAP+ADX", sid, cfg, research, logger, slippage, skipped,
        )
