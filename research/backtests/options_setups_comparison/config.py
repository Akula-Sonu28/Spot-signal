"""Research-only configuration for options setups comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bot.config import DEFAULT_CONFIG, StrategyConfig

ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_ROOT = Path(__file__).resolve().parent / "outputs"
HIST_DIR = ROOT / "data" / "historical"

DEFAULT_FROM = "2026-04-01"
DEFAULT_TO = "2026-06-09"

SETUP_IDS = ("A", "B", "C", "D", "E", "F", "G", "H")
SLIPPAGE_TIERS = ("none", "conservative", "stress")
RISK_ENGINE_MODES = ("production", "unified")


@dataclass(frozen=True)
class ResearchConfig:
    """Frozen research run configuration."""

    from_date: str = DEFAULT_FROM
    to_date: str = DEFAULT_TO
    futures_only: bool = True
    setup_ids: tuple[str, ...] = SETUP_IDS
    slippage_tiers: tuple[str, ...] = SLIPPAGE_TIERS
    risk_engine_mode: str = "unified"
    strategy: StrategyConfig = DEFAULT_CONFIG
    max_losses_per_day: int = 2
    gap_min_pct: float = 0.3
    compression_atr_ratio: float = 0.5
    retest_proximity_pts: float = 15.0
    swing_lookback: int = 5
    outputs_root: Path = OUTPUTS_ROOT

    @property
    def data_type(self) -> str:
        return "underlying_spot_proxy"

    @property
    def option_premium(self) -> bool:
        return False
