"""Setup J+ trap-fade entry rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.state import Position, PositionSide, ReplayState, make_day_state
from bot.strategy import BarContext, build_bar_context
from bot.strategy_j import (
    JTrapConfig,
    J_TRAP_ROBUST,
    _body_ratio,
    process_bar_j_entries,
    update_trap_flags,
)

TZ = ZoneInfo("Asia/Kolkata")


def _bar(
    hour: int,
    minute: int,
    o: float,
    h: float,
    l: float,
    c: float,
    *,
    vwap: float = 100.0,
    atr: float = 8.0,
    adx: float = 22.0,
    index: int = 10,
) -> BarContext:
    ts = datetime(2026, 3, 2, hour, minute, tzinfo=TZ)
    return build_bar_context(
        index, ts, "2026-03-02", o, h, l, c, 1000.0, vwap, atr, adx, StrategyConfig(), TZ
    )


def _wide_or_day() -> ReplayState:
    day = make_day_state("2026-03-02")
    day.or_high = 110.0
    day.or_low = 0.0
    day.or_defined = True
    day.day_mode = "J_PLUS"
    return ReplayState(day=day, position=Position())


def test_body_ratio_bearish() -> None:
    bar = _bar(10, 0, 104.0, 105.0, 99.0, 100.0)
    assert _body_ratio(bar, bullish=False) == 4.0 / 6.0


def test_j_trap_robust_stricter_than_base() -> None:
    base = JTrapConfig()
    robust = J_TRAP_ROBUST
    assert robust.min_trap_excess_pts > base.min_trap_excess_pts
    assert robust.max_trades_day <= base.max_trades_day
    assert robust.skip_both_trapped is True


def test_j_plus_max_one_trade_per_day() -> None:
    state = _wide_or_day()
    logger = ReplayLogger()
    cfg = StrategyConfig()
    jcfg = J_TRAP_ROBUST

    state.day.trades_today = 1
    bar = _bar(10, 30, 100.0, 112.0, 99.0, 98.0, vwap=102.0)
    process_bar_j_entries(state, bar, logger, cfg, jcfg)
    assert logger.filter("BUY_PE") == []


def test_j_plus_loss_cap_blocks_reentry() -> None:
    state = _wide_or_day()
    logger = ReplayLogger()
    cfg = StrategyConfig()
    jcfg = J_TRAP_ROBUST

    state.day.losses_today = 1
    bar = _bar(10, 30, 100.0, 112.0, 99.0, 98.0, vwap=102.0)
    process_bar_j_entries(state, bar, logger, cfg, jcfg)
    assert logger.filter("BUY_PE") == []


def test_j_plus_skip_both_trapped() -> None:
    state = _wide_or_day()
    logger = ReplayLogger()
    cfg = StrategyConfig()
    jcfg = J_TRAP_ROBUST

    state.day.touched_above_or = True
    state.day.touched_below_or = True
    state.day.trap_high = 120.0
    state.day.trap_low = -5.0

    bar = _bar(10, 30, 100.0, 121.0, 99.0, 98.0, vwap=102.0)
    process_bar_j_entries(state, bar, logger, cfg, jcfg)
    assert logger.filter("BUY_PE") == []
    assert logger.filter("BUY_CE") == []


def test_update_trap_flags_before_time_gate() -> None:
    day = make_day_state("2026-03-02")
    day.or_high = 110.0
    day.or_low = 0.0
    bar = _bar(9, 35, 100.0, 125.0, 99.0, 110.0)
    update_trap_flags(day, bar)
    assert day.touched_above_or is True
    assert day.trap_high == 125.0
    assert day.first_trap_side == "PE"


def test_j_plus_positive_pe_entry() -> None:
    state = _wide_or_day()
    logger = ReplayLogger()
    cfg = StrategyConfig()
    jcfg = J_TRAP_ROBUST
    state.day.touched_above_or = True
    state.day.trap_high = 125.0
    state.day.first_trap_side = "PE"

    bar = _bar(10, 30, 100.0, 101.0, 90.0, 91.0, vwap=102.0, adx=22.0)
    process_bar_j_entries(state, bar, logger, cfg, jcfg)
    assert len(logger.filter("BUY_PE")) == 1


def test_one_trap_side_only_blocks_opposite() -> None:
    state = _wide_or_day()
    logger = ReplayLogger()
    cfg = StrategyConfig()
    jcfg = JTrapConfig(
        min_trap_excess_pts=15.0,
        min_reclaim_pts=8.0,
        min_vwap_dist_pts=8.0,
        min_body_ratio=0.4,
        min_adx=20.0,
        min_minutes_after_or=0,
        max_trades_day=1,
        max_losses_day=1,
        one_trap_side_only=True,
    )
    state.day.touched_above_or = True
    state.day.touched_below_or = True
    state.day.trap_high = 125.0
    state.day.trap_low = -10.0
    state.day.first_trap_side = "PE"

    ce_bar = _bar(10, 30, 5.0, 15.0, 4.0, 12.0, vwap=8.0, adx=22.0)
    process_bar_j_entries(state, ce_bar, logger, cfg, jcfg)
    assert logger.filter("BUY_CE") == []
