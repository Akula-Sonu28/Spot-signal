"""Thin re-exports — canonical J+ logic lives in bot.strategy_j."""

from __future__ import annotations

from bot.config import CombinedStrategyConfig, StrategyConfig
from bot.logger import ReplayLogger
from bot.state import ReplayState
from bot.strategy import BarContext
from bot.strategy_j import JTrapConfig, J_TRAP_ROBUST, _body_ratio, process_bar_j_entries

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.slippage import SlippageModel

__all__ = [
    "JTrapConfig",
    "J_TRAP_ROBUST",
    "_body_ratio",
    "process_bar_j_entries",
    "process_or_fake_break_filtered",
]


def process_or_fake_break_filtered(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    jcfg: JTrapConfig = J_TRAP_ROBUST,
) -> None:
    """Legacy research adapter — delegates to production combined bar processor."""
    from bot.combined import process_session_bar

    _ = research, slippage, skipped
    replay = ReplayState(position=state.position, day=state.day, bar_index=state.bar_index)
    combined = CombinedStrategyConfig(strategy=cfg, enable_j_plus=True, j_trap=jcfg)
    process_session_bar(replay, bar, logger, combined)
    state.position = replay.position
    state.bar_index = replay.bar_index
