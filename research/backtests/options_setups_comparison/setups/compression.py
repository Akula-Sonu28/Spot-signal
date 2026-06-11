"""Setup E: Compression breakout."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import compression_width_ratio
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.setups.common import (
    adx_ok,
    standard_preamble,
    try_enter,
    vwap_long_ok,
    vwap_short_ok,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_compression_bar(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    prev_atr: float | None = None,
) -> None:
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    if highs is None or lows is None or bar.atr is None:
        return

    ratio = compression_width_ratio(highs, lows, bar.atr, bar.index, lookback=6)
    if ratio is None or ratio >= research.compression_atr_ratio:
        return

    body = abs(bar.close - bar.open)
    rng = bar.high - bar.low
    if rng <= 0 or body / rng < 0.5:
        return

    atr_expanding = prev_atr is not None and bar.atr > prev_atr
    if not atr_expanding:
        return

    comp_hi = max(highs[bar.index - 5 : bar.index + 1])
    comp_lo = min(lows[bar.index - 5 : bar.index + 1])

    if bar.close > comp_hi and vwap_long_ok(bar, cfg) and adx_ok(bar, cfg):
        try_enter(
            state, bar, PositionSide.CE, comp_lo,
            "COMPRESSION_BREAKOUT_CE", "E", cfg, research, logger, slippage, skipped,
        )
    elif bar.close < comp_lo and vwap_short_ok(bar, cfg) and adx_ok(bar, cfg):
        try_enter(
            state, bar, PositionSide.PE, comp_hi,
            "COMPRESSION_BREAKOUT_PE", "E", cfg, research, logger, slippage, skipped,
        )
