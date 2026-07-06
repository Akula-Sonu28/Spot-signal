"""Tests for advanced option-defense research sweeps."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import DEFAULT_CONFIG
from bot.logger import ReplayLogger, SignalEvent
from scripts.research_sweeps import (
    EXPIRY_ENTRY_CUTOFF_TIME,
    MAX_SPREAD_DRAG_PCT,
    TARGET_BUFFER_MULT_ATR,
    _entry_blocked,
    _is_expiry_day,
    _target_exit_blocked,
    filter_trade_events,
    run_advanced_sweep_matrix,
)

TZ = ZoneInfo("Asia/Kolkata")


def _buy(ts: datetime, side: str, bar: int, **extra) -> SignalEvent:
    return SignalEvent(
        timestamp=ts,
        event_type=f"BUY_{side}",
        direction="LONG" if side == "CE" else "SHORT",
        side=side,
        price=100.0,
        stop=90.0 if side == "CE" else 110.0,
        target=120.0 if side == "CE" else 80.0,
        bar_index=bar,
        extra=extra,
    )


def test_advanced_grid_dimensions():
    assert len(MAX_SPREAD_DRAG_PCT) == 4
    assert len(TARGET_BUFFER_MULT_ATR) == 3
    assert len(EXPIRY_ENTRY_CUTOFF_TIME) == 3
    assert len(MAX_SPREAD_DRAG_PCT) * len(TARGET_BUFFER_MULT_ATR) * len(EXPIRY_ENTRY_CUTOFF_TIME) == 36


def test_spread_drag_blocks_high_friction_entry():
    ev = _buy(datetime(2026, 6, 10, 10, 0, tzinfo=TZ), "CE", 5, spread_drag_pct=4.5)
    assert _entry_blocked(ev, max_spread_drag_pct=3.0, j_streaks={}, cfg=DEFAULT_CONFIG)
    assert not _entry_blocked(ev, max_spread_drag_pct=5.0, j_streaks={}, cfg=DEFAULT_CONFIG)


def test_expiry_cutoff_blocks_tuesday_afternoon_entry():
    # 2026-06-09 is Tuesday
    late = datetime(2026, 6, 9, 12, 30, tzinfo=TZ)  # close 12:35
    ev = _buy(late, "PE", 20, is_expiry_day=True)
    assert _is_expiry_day("2026-06-09")
    assert _entry_blocked(ev, expiry_entry_cutoff="12:00", j_streaks={}, cfg=DEFAULT_CONFIG)
    early = datetime(2026, 6, 9, 11, 50, tzinfo=TZ)  # close 11:55
    assert not _entry_blocked(_buy(early, "PE", 19, is_expiry_day=True), expiry_entry_cutoff="12:00", j_streaks={}, cfg=DEFAULT_CONFIG)


def test_expiry_cutoff_ignored_on_non_expiry_day():
    # 2026-06-10 is Wednesday
    late = datetime(2026, 6, 10, 14, 0, tzinfo=TZ)
    ev = _buy(late, "CE", 30, is_expiry_day=False)
    assert not _entry_blocked(ev, expiry_entry_cutoff="12:00", j_streaks={}, cfg=DEFAULT_CONFIG)


def test_target_buffer_defers_weak_target_exit():
    entry = _buy(datetime(2026, 6, 9, 10, 0, tzinfo=TZ), "CE", 10, atr=10.0)
    target_ev = SignalEvent(
        timestamp=datetime(2026, 6, 9, 10, 30, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=120.0,
        stop=90.0,
        target=120.0,
        bar_index=16,
        extra={"bar_high": 120.5, "bar_low": 115.0, "bar_close": 119.0, "atr": 10.0},
    )
    assert _target_exit_blocked(target_ev, target_buffer_mult_atr=0.1, open_entry=entry)
    target_ev.extra["bar_high"] = 121.5
    assert not _target_exit_blocked(target_ev, target_buffer_mult_atr=0.1, open_entry=entry)


def test_filter_spread_and_expiry_drop_round_trip():
    entry = datetime(2026, 6, 9, 12, 30, tzinfo=TZ)
    exit_ts = datetime(2026, 6, 9, 15, 0, tzinfo=TZ)
    events = [
        _buy(entry, "CE", 50, spread_drag_pct=5.0, is_expiry_day=True),
        SignalEvent(
            timestamp=exit_ts,
            event_type="SL_CE",
            direction="LONG_EXIT",
            side="CE",
            price=90.0,
            reason="STOP_HIT",
            bar_index=55,
        ),
    ]
    filtered = filter_trade_events(
        events,
        max_spread_drag_pct=3.0,
        expiry_entry_cutoff="12:00",
        j_streaks={},
        cfg=DEFAULT_CONFIG,
    )
    assert filtered == []


def test_run_advanced_sweep_matrix_empty_replay():
    logger = ReplayLogger()
    df = pd.DataFrame(columns=["session_date", "timestamp", "open", "high", "low", "close", "volume"])
    cells = run_advanced_sweep_matrix(logger, df, 0)
    assert len(cells) == 36
    assert cells[0].total_trades == 0
