"""Unit tests for strategy rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import DayState, Position, PositionSide, ReplayState, make_day_state
from bot.strategy import BarContext, _calc_stops, build_bar_context, process_bar, update_or

TZ = ZoneInfo("Asia/Kolkata")


def _bar(
    index: int,
    hour: int,
    minute: int,
    o: float,
    h: float,
    l: float,
    c: float,
    session: str = "2025-01-07",
    vwap: float | None = 24000.0,
    atr: float = 20.0,
    adx: float = 20.0,
) -> BarContext:
    ts = datetime(2025, 1, 7, hour, minute, tzinfo=TZ)
    return build_bar_context(
        index, ts, session, o, h, l, c, 100000, vwap, atr, adx, StrategyConfig()
    )


def test_or_accumulates_pine_window():
    day = make_day_state("2025-01-07")
    cfg = StrategyConfig()
    # Bar open labels 9:15 and 9:20 fall in OR; 9:25 open closes at 9:30 (excluded)
    bars = [
        _bar(0, 9, 15, 24120, 24150, 24100, 24130),
        _bar(1, 9, 20, 24130, 24155, 24105, 24140),
        _bar(2, 9, 25, 24140, 24160, 24110, 24150),
    ]
    for b in bars:
        update_or(day, b, cfg)
    assert day.or_high == 24155
    assert day.or_low == 24100
    assert day.or_bars_seen == 2


def test_or_width_blocks_narrow_range():
    cfg = StrategyConfig(min_or_range=25, max_or_range=100)
    day = DayState(session_date="x", or_high=24110, or_low=24100, or_defined=True)
    logger = ReplayLogger()
    state = ReplayState(day=day, position=Position())
    bar = _bar(5, 10, 0, 24120, 24125, 24115, 24122, adx=20)
    process_bar(state, bar, logger, cfg)
    assert logger.filter("BUY_CE") == []


def test_calc_stops_atr_mode():
    cfg = StrategyConfig(atr_sl_mult=1.2, atr_target_mult=1.2, rr_ratio=1.8)
    stop, target = _calc_stops(PositionSide.CE, 100.0, 10.0, 95.0, 90.0, cfg)
    assert stop == pytest.approx(88.0)  # 100 - 1.2*10
    assert target == pytest.approx(121.6)  # 100 + 10*1.2*1.8


def test_calc_stops_decoupled_sl_wider_than_target_base():
    cfg = StrategyConfig(atr_sl_mult=1.5, atr_target_mult=1.2, rr_ratio=1.8)
    stop, target = _calc_stops(PositionSide.CE, 100.0, 10.0, 95.0, 90.0, cfg)
    assert stop == pytest.approx(85.0)
    assert target == pytest.approx(121.6)


def test_calc_stops_disabled():
    cfg = StrategyConfig(use_stop_loss=False, atr_target_mult=1.2, rr_ratio=2.0)
    stop, target = _calc_stops(PositionSide.CE, 100.0, 10.0, 95.0, 90.0, cfg)
    assert stop is None
    assert target == pytest.approx(124.0)


def test_buy_ce_on_breakout():
    cfg = StrategyConfig()
    day = DayState(
        session_date="2025-01-07",
        or_high=24150,
        or_low=24100,
        or_defined=True,
        or_bars_seen=3,
    )
    logger = ReplayLogger()
    state = ReplayState(day=day, position=Position())
    bar = _bar(5, 10, 0, 24155, 24170, 24152, 24165, vwap=24140, adx=18, atr=15)
    process_bar(state, bar, logger, cfg)
    entries = logger.filter("BUY_CE")
    assert len(entries) == 1
    assert entries[0].price == 24165
    assert state.position.side == PositionSide.CE


def test_max_one_long_per_day():
    cfg = StrategyConfig(max_trades_per_day=2)
    day = DayState(
        session_date="2025-01-07",
        or_high=24150,
        or_low=24100,
        or_defined=True,
        fired_long_today=True,
        trades_today=1,
    )
    logger = ReplayLogger()
    state = ReplayState(day=day, position=Position())
    bar = _bar(6, 10, 5, 24160, 24180, 24158, 24175, vwap=24140, adx=18)
    process_bar(state, bar, logger, cfg)
    assert logger.filter("BUY_CE") == []


def test_square_off_closes_position():
    cfg = StrategyConfig()
    day = make_day_state("2025-01-07")
    day.or_defined = True
    pos = Position(
        side=PositionSide.CE,
        entry_price=24100,
        stop=24080,
        target=24150,
    )
    logger = ReplayLogger()
    state = ReplayState(day=day, position=pos)
    bar = _bar(70, 15, 15, 24120, 24125, 24115, 24122)
    process_bar(state, bar, logger, cfg)
    assert logger.filter("SQUARE_OFF")
    assert state.position.side == PositionSide.FLAT


def test_sl_priority_over_target_same_bar():
    cfg = StrategyConfig()
    day = make_day_state("2025-01-07")
    day.or_defined = True
    pos = Position(
        side=PositionSide.CE,
        entry_price=100,
        stop=95,
        target=110,
    )
    logger = ReplayLogger()
    state = ReplayState(day=day, position=pos)
    bar = _bar(10, 10, 30, 100, 115, 94, 105)
    process_bar(state, bar, logger, cfg)
    assert logger.filter("SL_CE")
    assert not logger.filter("TARGET_CE")
