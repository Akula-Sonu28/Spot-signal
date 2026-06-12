"""Day-mode router: v3.8 vs J+ vs skip (mutually exclusive per session)."""

from __future__ import annotations

from enum import Enum

from bot.config import StrategyConfig
from bot.indicators import or_width
from bot.state import DayState


class DayMode(str, Enum):
    SKIP = "SKIP"
    V38 = "V38"
    J_PLUS = "J_PLUS"


def resolve_day_mode(day: DayState, cfg: StrategyConfig) -> DayMode | None:
    """Return day mode once OR is defined; None if OR not ready."""
    if not day.or_defined:
        return None
    width = or_width(day.or_high, day.or_low)
    if width is None:
        return None
    if width < cfg.min_or_range:
        return DayMode.SKIP
    if width <= cfg.max_or_range:
        return DayMode.V38
    return DayMode.J_PLUS


def ensure_day_mode(day: DayState, cfg: StrategyConfig) -> DayMode | None:
    """Set day.day_mode once when OR becomes defined."""
    if day.day_mode is not None:
        return DayMode(day.day_mode)
    mode = resolve_day_mode(day, cfg)
    if mode is not None:
        day.day_mode = mode.value
    return mode
