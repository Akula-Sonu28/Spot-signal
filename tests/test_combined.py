"""Combined v3.9 router — mutual exclusion per session."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.combined import process_session_bar
from bot.config import CombinedStrategyConfig, StrategyConfig
from bot.day_router import DayMode
from bot.logger import ReplayLogger
from bot.state import Position, ReplayState, make_day_state
from bot.strategy import BarContext, build_bar_context, update_or
from bot.strategy_j import J_TRAP_ROBUST

TZ = ZoneInfo("Asia/Kolkata")
CFG = StrategyConfig(min_or_range=25.0, max_or_range=100.0)
COMBINED = CombinedStrategyConfig(strategy=CFG, enable_j_plus=True, j_trap=J_TRAP_ROBUST)


def _bar(
    hour: int,
    minute: int,
    o: float,
    h: float,
    l: float,
    c: float,
    *,
    vwap: float = 100.0,
    adx: float = 22.0,
    index: int = 5,
) -> BarContext:
    ts = datetime(2026, 3, 2, hour, minute, tzinfo=TZ)
    return build_bar_context(
        index, ts, "2026-03-02", o, h, l, c, 1000.0, vwap, 8.0, adx, CFG, TZ
    )


def test_wide_or_day_mode_is_j_plus_no_v38_breakout() -> None:
    day = make_day_state("2026-03-02")
    day.or_high = 150.0
    day.or_low = 40.0
    day.or_defined = True
    day.day_mode = DayMode.J_PLUS.value

    state = ReplayState(day=day, position=Position())
    logger = ReplayLogger()
    bar = _bar(10, 0, 149.0, 155.0, 148.0, 154.0, vwap=145.0)

    process_session_bar(state, bar, logger, COMBINED)
    assert logger.filter("BUY_CE") == []


def test_valid_or_day_mode_v38_not_j_plus() -> None:
    day = make_day_state("2026-03-02")
    day.or_high = 80.0
    day.or_low = 50.0
    day.or_defined = True
    day.day_mode = DayMode.V38.value

    state = ReplayState(day=day, position=Position())
    logger = ReplayLogger()

    trap_bar = _bar(10, 30, 78.0, 82.0, 48.0, 79.0, vwap=77.0)
    process_session_bar(state, trap_bar, logger, COMBINED)
    assert logger.filter("BUY_CE") == []
    assert logger.filter("BUY_PE") == []


def test_router_sets_mode_once_from_or() -> None:
    day = make_day_state("2026-03-02")
    state = ReplayState(day=day, position=Position())
    logger = ReplayLogger()

    for b in [
        _bar(9, 15, 55.0, 70.0, 50.0, 65.0, index=0),
        _bar(9, 20, 65.0, 85.0, 55.0, 80.0, index=1),
        _bar(9, 25, 80.0, 88.0, 78.0, 85.0, index=2),
    ]:
        update_or(day, b, CFG)
    assert day.day_mode is None

    close_or = _bar(9, 30, 75.0, 82.0, 74.0, 80.0, index=3)
    process_session_bar(state, close_or, logger, COMBINED)
    assert day.day_mode == DayMode.V38.value


def test_trap_tracking_via_session_bar_before_time_gate() -> None:
    day = make_day_state("2026-03-02")
    day.or_high = 110.0
    day.or_low = 0.0
    day.or_defined = True
    day.day_mode = DayMode.J_PLUS.value

    state = ReplayState(day=day, position=Position())
    logger = ReplayLogger()
    early = _bar(9, 35, 100.0, 125.0, 99.0, 110.0, index=4)

    process_session_bar(state, early, logger, COMBINED)
    assert day.touched_above_or is True
    assert day.trap_high == 125.0


def test_enable_j_plus_false_blocks_wide_or_entries() -> None:
    day = make_day_state("2026-03-02")
    day.or_high = 150.0
    day.or_low = 40.0
    day.or_defined = True
    day.day_mode = DayMode.J_PLUS.value
    day.touched_above_or = True
    day.trap_high = 170.0
    day.first_trap_side = "PE"

    disabled = CombinedStrategyConfig(strategy=CFG, enable_j_plus=False, j_trap=J_TRAP_ROBUST)
    state = ReplayState(day=day, position=Position())
    logger = ReplayLogger()
    bar = _bar(10, 30, 145.0, 146.0, 135.0, 136.0, vwap=140.0, adx=22.0)

    process_session_bar(state, bar, logger, disabled)
    assert logger.filter("BUY_PE") == []
    assert logger.filter("BUY_CE") == []
