"""Stop and target calculation for v3.9 (v3.8 OR stops + J+ capped structure stops)."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.state import PositionSide
from bot.strategy import _calc_stops, _calc_target


def capped_stops(
    side: PositionSide,
    entry: float,
    structure_stop: float,
    atr: float,
    cfg: StrategyConfig,
) -> tuple[float, float]:
    """J+ entry: structure stop capped at max ATR distance; target at RR × risk."""
    max_dist = atr * cfg.atr_sl_mult
    if side == PositionSide.CE:
        stop = max(structure_stop, entry - max_dist)
        risk = entry - stop
        if risk <= 0:
            target = _calc_target(side, entry, atr, cfg)
            return stop, target
        return stop, entry + risk * cfg.rr_ratio
    stop = min(structure_stop, entry + max_dist)
    risk = stop - entry
    if risk <= 0:
        target = _calc_target(side, entry, atr, cfg)
        return stop, target
    return stop, entry - risk * cfg.rr_ratio


def v38_stops(
    side: PositionSide,
    entry: float,
    atr: float,
    or_high: float,
    or_low: float,
    cfg: StrategyConfig,
) -> tuple[float | None, float]:
    """v3.8 OR-boundary stop + decoupled ATR target."""
    return _calc_stops(side, entry, atr, or_high, or_low, cfg)
