"""Tests for Setup J robustness filters."""

from __future__ import annotations

from bot.strategy_j import JTrapConfig, J_TRAP_ROBUST, _body_ratio
from bot.strategy import BarContext
from datetime import datetime
from zoneinfo import ZoneInfo


def _bar(**kwargs) -> BarContext:
    defaults = dict(
        index=10,
        timestamp=datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        session_date="2026-03-02",
        open=100.0,
        high=105.0,
        low=99.0,
        close=101.0,
        volume=1000.0,
        vwap=102.0,
        atr=5.0,
        adx=22.0,
        is_after_or=True,
        is_entry_window=True,
        is_square_off=False,
        in_or=False,
    )
    defaults.update(kwargs)
    return BarContext(**defaults)


def test_body_ratio_bullish() -> None:
    bar = _bar(open=100.0, close=104.0, high=105.0, low=99.0)
    assert _body_ratio(bar, bullish=True) == 4.0 / 6.0


def test_j_trap_robust_is_stricter_than_base() -> None:
    base = JTrapConfig()
    robust = J_TRAP_ROBUST
    assert robust.min_trap_excess_pts > base.min_trap_excess_pts
    assert robust.max_trades_day <= base.max_trades_day
    assert robust.skip_both_trapped is True
