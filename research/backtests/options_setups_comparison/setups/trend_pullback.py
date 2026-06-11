"""Setup G: Trend-day pullback."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import vwap_slope
from research.backtests.options_setups_comparison.risk_engine import (
    PendingEntry,
    ResearchState,
    SkippedLogger,
)
from research.backtests.options_setups_comparison.setups.common import (
    adx_ok,
    flags,
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel


def process_trend_pullback_bar(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    ema9: float | None = None,
    ema20: float | None = None,
    vwaps: list[float | None] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> None:
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    if bar.vwap is None or highs is None or lows is None or vwaps is None:
        return

    f = flags(state)
    slope = vwap_slope(vwaps, bar.index, 3)

    if state.pending is not None:
        pend = state.pending
        if pend.side == PositionSide.CE and bar.high >= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.CE, pend.structure_stop,
                pend.reason, "G", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        elif pend.side == PositionSide.PE and bar.low <= pend.trigger_price:
            state.pending = None
            try_enter(
                state, bar, PositionSide.PE, pend.structure_stop,
                pend.reason, "G", cfg, research, logger, slippage, skipped,
                entry_price=pend.trigger_price,
            )
        elif bar.index > pend.set_bar_index + 2:
            state.pending = None

    idx = bar.index
    if idx < 4:
        return

    hh = highs[idx] > highs[idx - 1] and highs[idx - 1] > highs[idx - 2]
    hl = lows[idx] > lows[idx - 1] and lows[idx - 1] > lows[idx - 2]
    lh = highs[idx] < highs[idx - 1] and highs[idx - 1] < highs[idx - 2]
    ll = lows[idx] < lows[idx - 1] and lows[idx - 1] < lows[idx - 2]

    near_pullback_ce = (
        ema9 is not None and bar.low <= ema9 <= bar.high
    ) or (
        ema20 is not None and bar.low <= ema20 <= bar.high
    ) or (bar.low <= bar.vwap <= bar.high)

    weak_sell = bar.close > bar.open and bar.close > (bar.low + bar.high) / 2

    if (
        bar.close > bar.vwap
        and slope is not None and slope > 0
        and hh and hl
        and near_pullback_ce
        and weak_sell
        and adx_ok(bar, cfg)
        and state.pending is None
    ):
        state.pending = PendingEntry(
            side=PositionSide.CE,
            trigger_price=bar.high,
            structure_stop=bar.low - 1,
            reason="TREND_PULLBACK_CE",
            set_bar_index=bar.index,
        )

    near_pullback_pe = near_pullback_ce
    weak_buy = bar.close < bar.open and bar.close < (bar.low + bar.high) / 2

    if (
        bar.close < bar.vwap
        and slope is not None and slope < 0
        and lh and ll
        and near_pullback_pe
        and weak_buy
        and adx_ok(bar, cfg)
        and state.pending is None
    ):
        state.pending = PendingEntry(
            side=PositionSide.PE,
            trigger_price=bar.low,
            structure_stop=bar.high + 1,
            reason="TREND_PULLBACK_PE",
            set_bar_index=bar.index,
        )
