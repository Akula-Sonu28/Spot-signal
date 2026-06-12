"""Multi-confirmation setups for wide-OR / gap non-trade days (research only)."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import (
    bar_minutes_ist,
    volume_ratio,
)
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.setups.common import (
    adx_ok,
    flags,
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel

# Wide OR = production skip band; gap literature uses 40–50 NIFTY points
WIDE_OR_MIN = 100.0
GAP_MIN_PTS = 40.0


def _is_wide_or(day) -> bool:
    w = or_width(day.or_high, day.or_low)
    return w is not None and w > WIDE_OR_MIN


def process_or_fake_break_stack(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
) -> None:
    """J: Wick above OR + close back inside + below VWAP + ADX (ORB trap stack)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    if bar.vwap is None or day.or_high is None or day.or_low is None:
        return

    f = flags(state)
    or_high = day.or_high
    or_low = day.or_low

    if bar.high > or_high and not f.get("touched_above_or"):
        f["touched_above_or"] = True
        f["trap_high"] = bar.high
    if bar.low < or_low and not f.get("touched_below_or"):
        f["touched_below_or"] = True
        f["trap_low"] = bar.low

    if (
        f.get("touched_above_or")
        and bar.close < or_high
        and bar.close < bar.vwap
        and adx_ok(bar, cfg)
        and bar.close < bar.open
    ):
        stop = max(f.get("trap_high", bar.high), bar.high) + cfg.sl_buffer_pts
        try_enter(
            state, bar, PositionSide.PE, stop,
            "OR_FAKE_BREAK_PE", "J", cfg, research, logger, slippage, skipped,
        )

    if (
        f.get("touched_below_or")
        and bar.close > or_low
        and bar.close > bar.vwap
        and adx_ok(bar, cfg)
        and bar.close > bar.open
    ):
        stop = min(f.get("trap_low", bar.low), bar.low) - cfg.sl_buffer_pts
        try_enter(
            state, bar, PositionSide.CE, stop,
            "OR_FAKE_BREAK_CE", "J", cfg, research, logger, slippage, skipped,
        )


def process_gap_fade_stack(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    rsi: float | None = None,
) -> None:
    """K: Gap≥40pts + OR bearish/bullish + VWAP fail + vs open + RSI (4–5 confirms)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    prior = state.prior_close
    if prior is None or bar.vwap is None:
        return

    f = flags(state)
    if bar.in_or:
        f.setdefault("or_open", bar.open)
        f["or_close"] = bar.close
    if not f.get("gap_set") and not bar.in_or and f.get("or_open") is not None:
        gap_pts = f["or_open"] - prior
        f["gap_pts"] = gap_pts
        f["gap_up"] = gap_pts >= GAP_MIN_PTS
        f["gap_down"] = gap_pts <= -GAP_MIN_PTS
        f["or_bearish"] = f.get("or_close", bar.open) < f["or_open"]
        f["or_bullish"] = f.get("or_close", bar.open) > f["or_open"]
        f["gap_set"] = True

    if not f.get("gap_set"):
        return

    # Gap-up fade: OR red + lose VWAP + below open + RSI not oversold
    if (
        f.get("gap_up")
        and f.get("or_bearish")
        and bar.close < bar.vwap
        and bar.close < f["or_open"]
        and (rsi is None or rsi > 45)
        and adx_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.PE, bar.high + cfg.sl_buffer_pts,
            "GAP_FADE_STACK_PE", "K", cfg, research, logger, slippage, skipped,
        )

    if (
        f.get("gap_down")
        and f.get("or_bullish")
        and bar.close > bar.vwap
        and bar.close > f["or_open"]
        and (rsi is None or rsi < 55)
        and adx_ok(bar, cfg)
    ):
        try_enter(
            state, bar, PositionSide.CE, bar.low - cfg.sl_buffer_pts,
            "GAP_FADE_STACK_CE", "K", cfg, research, logger, slippage, skipped,
        )


def process_gap_go_stack(
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
) -> None:
    """L: Gap≥40pts + OR bullish + VWAP + EMA9>EMA20 + ADX≥20 + OR break (5 confirms)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    prior = state.prior_close
    if prior is None or bar.vwap is None or day.or_high is None or day.or_low is None:
        return

    f = flags(state)
    if bar.in_or:
        f.setdefault("or_open", bar.open)
        f["or_close"] = bar.close
    if not f.get("gap_set") and not bar.in_or and f.get("or_open") is not None:
        gap_pts = f["or_open"] - prior
        f["gap_pts"] = gap_pts
        f["gap_up"] = gap_pts >= GAP_MIN_PTS
        f["gap_down"] = gap_pts <= -GAP_MIN_PTS
        f["or_bullish"] = f.get("or_close", bar.open) > f["or_open"]
        f["or_bearish"] = f.get("or_close", bar.open) < f["or_open"]
        f["gap_set"] = True

    if not f.get("gap_set") or ema9 is None or ema20 is None:
        return

    adx_strong = bar.adx is not None and bar.adx >= 20.0

    if (
        f.get("gap_up")
        and f.get("or_bullish")
        and bar.close > bar.vwap
        and ema9 > ema20
        and ema20 > bar.vwap
        and adx_strong
        and bar.close > day.or_high
    ):
        try_enter(
            state, bar, PositionSide.CE, day.or_low - cfg.sl_buffer_pts,
            "GAP_GO_STACK_CE", "L", cfg, research, logger, slippage, skipped,
        )

    if (
        f.get("gap_down")
        and f.get("or_bearish")
        and bar.close < bar.vwap
        and ema9 < ema20
        and ema20 < bar.vwap
        and adx_strong
        and bar.close < day.or_low
    ):
        try_enter(
            state, bar, PositionSide.PE, day.or_high + cfg.sl_buffer_pts,
            "GAP_GO_STACK_PE", "L", cfg, research, logger, slippage, skipped,
        )


def process_vwap_extension_fade(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    rsi: float | None = None,
) -> None:
    """M: Midday 11–14 + price >1.2 ATR from VWAP + RSI extreme (mean reversion)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    if bar.vwap is None or bar.atr is None or rsi is None:
        return

    mins = bar_minutes_ist(bar.timestamp)
    if mins < 11 * 60 or mins >= 14 * 60:
        return

    dist = bar.close - bar.vwap
    threshold = bar.atr * 1.2
    chop = bar.adx is not None and bar.adx < 25.0

    if dist > threshold and rsi > 62 and chop:
        try_enter(
            state, bar, PositionSide.PE, bar.high + cfg.sl_buffer_pts,
            "VWAP_EXT_FADE_PE", "M", cfg, research, logger, slippage, skipped,
        )
    if dist < -threshold and rsi < 38 and chop:
        try_enter(
            state, bar, PositionSide.CE, bar.low - cfg.sl_buffer_pts,
            "VWAP_EXT_FADE_CE", "M", cfg, research, logger, slippage, skipped,
        )


def process_orb_ema_volume_stack(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    ema20: float | None = None,
    vol_ratio: float | None = None,
) -> None:
    """N: OR break + VWAP + EMA20>VWAP + ADX + volume≥1.2× (Sahi ORB stack)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    if bar.vwap is None or day.or_high is None or day.or_low is None or ema20 is None:
        return
    if vol_ratio is not None and vol_ratio < 1.2:
        return
    if not adx_ok(bar, cfg):
        return

    if bar.close > day.or_high and bar.close > bar.vwap and ema20 > bar.vwap:
        try_enter(
            state, bar, PositionSide.CE, day.or_low - cfg.sl_buffer_pts,
            "ORB_EMA_VOL_CE", "N", cfg, research, logger, slippage, skipped,
        )
    elif bar.close < day.or_low and bar.close < bar.vwap and ema20 < bar.vwap:
        try_enter(
            state, bar, PositionSide.PE, day.or_high + cfg.sl_buffer_pts,
            "ORB_EMA_VOL_PE", "N", cfg, research, logger, slippage, skipped,
        )


def process_vwap_reclaim_volume_stack(
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
    vol_ratio: float | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> None:
    """O: 3 bars below VWAP → reclaim + vol≥1.5× + EMA9>EMA20 + ADX rising + swing break."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _is_wide_or(day):
        return
    if bar.vwap is None or highs is None or lows is None or ema9 is None or ema20 is None:
        return

    f = flags(state)
    below = f.get("below_streak", 0)
    above = f.get("above_streak", 0)
    if bar.close < bar.vwap:
        below += 1
        above = 0
    elif bar.close > bar.vwap:
        above += 1
        below = 0
    f["below_streak"] = below
    f["above_streak"] = above

    prev_adx = f.get("prev_adx")
    adx_rising = prev_adx is not None and bar.adx is not None and bar.adx >= prev_adx
    f["prev_adx"] = bar.adx

    idx = bar.index
    if idx < 5:
        return
    swing_hi = max(highs[idx - 4 : idx + 1])
    swing_lo = min(lows[idx - 4 : idx + 1])

    vol_ok = vol_ratio is None or vol_ratio >= 1.5

    if (
        below >= 3
        and f.get("was_below")
        and bar.close > bar.vwap
        and ema9 > ema20
        and adx_rising
        and adx_ok(bar, cfg)
        and vol_ok
        and bar.close > swing_hi
    ):
        try_enter(
            state, bar, PositionSide.CE, bar.low - cfg.sl_buffer_pts,
            "VWAP_RECLAIM_VOL_CE", "O", cfg, research, logger, slippage, skipped,
        )

    if below >= 3:
        f["was_below"] = True

    if (
        above >= 3
        and f.get("was_above")
        and bar.close < bar.vwap
        and ema9 < ema20
        and adx_rising
        and adx_ok(bar, cfg)
        and vol_ok
        and bar.close < swing_lo
    ):
        try_enter(
            state, bar, PositionSide.PE, bar.high + cfg.sl_buffer_pts,
            "VWAP_RECLAIM_VOL_PE", "O", cfg, research, logger, slippage, skipped,
        )

    if above >= 3:
        f["was_above"] = True


MULTI_SETUPS: dict[str, tuple[str, str]] = {
    "J": ("or_fake_break_stack", "OR trap: wick OR + close inside + VWAP"),
    "K": ("gap_fade_stack", "Gap fade: gap+OR color+VWAP+open+RSI"),
    "L": ("gap_go_stack", "Gap go: gap+OR+VWAP+EMA+ADX+break"),
    "M": ("vwap_extension_fade", "Midday VWAP stretch + RSI fade"),
    "N": ("orb_ema_volume_stack", "ORB+VWAP+EMA20+vol+ADX"),
    "O": ("vwap_reclaim_volume_stack", "VWAP reclaim+vol+EMA+ADX"),
}
