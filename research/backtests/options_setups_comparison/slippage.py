"""Slippage tiers for research backtests (not applied in production)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.state import PositionSide

SLIPPAGE_PTS: dict[str, float] = {
    "none": 0.0,
    "conservative": 2.0,
    "stress": 5.0,
}


@dataclass(frozen=True)
class SlippageModel:
    tier: str = "none"

    @property
    def pts(self) -> float:
        return SLIPPAGE_PTS.get(self.tier.lower(), 0.0)

    def adjust_entry(self, side: PositionSide, price: float) -> float:
        """Worse fill on entry."""
        if self.pts <= 0:
            return price
        if side == PositionSide.CE:
            return price + self.pts
        return price - self.pts

    def adjust_exit(self, side: PositionSide, price: float) -> float:
        """Worse fill on exit."""
        if self.pts <= 0:
            return price
        if side == PositionSide.CE:
            return price - self.pts
        return price + self.pts
