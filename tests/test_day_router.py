"""Day-mode router boundary tests."""

from __future__ import annotations

from bot.config import StrategyConfig
from bot.day_router import DayMode, ensure_day_mode, resolve_day_mode
from bot.state import make_day_state


def _day_with_or(high: float, low: float):
    day = make_day_state("2026-03-02")
    day.or_high = high
    day.or_low = low
    day.or_defined = True
    return day


def test_resolve_day_mode_boundaries() -> None:
    cfg = StrategyConfig(min_or_range=25.0, max_or_range=100.0)

    assert resolve_day_mode(_day_with_or(100.0, 76.0), cfg) == DayMode.SKIP
    assert resolve_day_mode(_day_with_or(100.0, 75.0), cfg) == DayMode.V38
    assert resolve_day_mode(_day_with_or(200.0, 100.0), cfg) == DayMode.V38
    assert resolve_day_mode(_day_with_or(201.0, 100.0), cfg) == DayMode.J_PLUS


def test_ensure_day_mode_sets_once() -> None:
    cfg = StrategyConfig(min_or_range=25.0, max_or_range=100.0)
    day = _day_with_or(150.0, 40.0)

    first = ensure_day_mode(day, cfg)
    assert first == DayMode.J_PLUS
    assert day.day_mode == "J_PLUS"

    day.or_high = 80.0
    day.or_low = 50.0
    second = ensure_day_mode(day, cfg)
    assert second == DayMode.J_PLUS
    assert day.day_mode == "J_PLUS"
