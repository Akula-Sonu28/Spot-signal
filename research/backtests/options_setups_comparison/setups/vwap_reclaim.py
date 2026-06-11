"""Setup D: VWAP reclaim / reject."""

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
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_vwap_reclaim_bar(
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
) -> None:
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    if bar.vwap is None or bar.adx is None:
        return

    f = flags(state)
    below_streak = f.get("below_vwap_streak", 0)
    above_streak = f.get("above_vwap_streak", 0)

    if bar.close < bar.vwap:
        below_streak += 1
        above_streak = 0
    elif bar.close > bar.vwap:
        above_streak += 1
        below_streak = 0

    f["below_vwap_streak"] = below_streak
    f["above_vwap_streak"] = above_streak
    prev_adx = f.get("prev_adx")
    adx_rising = prev_adx is not None and bar.adx >= prev_adx
    f["prev_adx"] = bar.adx

    idx = bar.index
    if highs is None or lows is None or idx < research.swing_lookback:
        return
    swing_hi = max(highs[idx - research.swing_lookback + 1 : idx + 1])
    swing_lo = min(lows[idx - research.swing_lookback + 1 : idx + 1])

    # CE: was below VWAP, reclaimed, holds, breaks swing high
    if (
        f.get("was_below_vwap")
        and bar.close > bar.vwap
        and f.get("hold_above")
        and bar.close > swing_hi
        and (adx_rising or (bar.atr and f.get("prev_atr") and bar.atr > f["prev_atr"]))
        and adx_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.CE, bar.low,
            "VWAP_RECLAIM_CE", "D", cfg, research, logger, slippage, skipped,
        )

    if bar.close > bar.vwap and f.get("was_below_vwap"):
        f["hold_above"] = True

    if below_streak >= 3:
        f["was_below_vwap"] = True
        f["hold_above"] = False

    # PE: mirror
    if (
        f.get("was_above_vwap")
        and bar.close < bar.vwap
        and f.get("hold_below")
        and bar.close < swing_lo
        and (adx_rising or (bar.atr and f.get("prev_atr") and bar.atr > f["prev_atr"]))
        and adx_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.PE, bar.high,
            "VWAP_REJECT_PE", "D", cfg, research, logger, slippage, skipped,
        )

    if bar.close < bar.vwap and f.get("was_above_vwap"):
        f["hold_below"] = True

    if above_streak >= 3:
        f["was_above_vwap"] = True
        f["hold_below"] = False

    f["prev_atr"] = bar.atr
