"""Setup A: production baseline via bot.strategy.process_bar."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import ReplayState
from bot.strategy import process_bar

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.slippage import SlippageModel
from bot.strategy import BarContext


def process_baseline_bar(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    use_production: bool = True,
) -> None:
    """Delegate to production process_bar (unified risk not applied when use_production=True)."""
    replay = ReplayState(position=state.position, day=state.day, bar_index=state.bar_index)
    process_bar(replay, bar, logger, cfg)
    state.position = replay.position
    state.day = replay.day  # type: ignore[assignment]
    state.bar_index = replay.bar_index
