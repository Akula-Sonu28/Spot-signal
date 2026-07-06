"""Tests for scripts/research_sweeps.py — in-memory post-filter sweep."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_CONFIG
from bot.logger import SignalEvent
from bot.strategy_audit import max_consecutive_bars_outside_or
from scripts.research_sweeps import (
    TIME_GATES,
    MAX_BARS_OUTSIDE,
    _entry_blocked,
    _parse_gate_minutes,
    filter_trade_events,
    run_sweep_matrix,
)
from bot.logger import ReplayLogger
from bot.backtest import pair_trades

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


def test_matrix_dimensions():
    assert len(TIME_GATES) == 4
    assert len(MAX_BARS_OUTSIDE) == 4


def test_time_gate_blocks_late_entry():
    late = datetime(2026, 6, 9, 14, 35, tzinfo=TZ)  # bar open → close 14:40
    ev = _buy(late, "CE", 50)
    assert _entry_blocked(ev, time_gate="13:30", max_bars_outside_or=None, j_streaks={}, cfg=DEFAULT_CONFIG)
    assert _entry_blocked(ev, time_gate="14:30", max_bars_outside_or=None, j_streaks={}, cfg=DEFAULT_CONFIG)
    early = datetime(2026, 6, 9, 14, 20, tzinfo=TZ)  # close 14:25
    assert not _entry_blocked(_buy(early, "CE", 49), time_gate="14:30", max_bars_outside_or=None, j_streaks={}, cfg=DEFAULT_CONFIG)


def test_j_plus_cap_blocks_long_outside_streak():
    entry_ts = datetime(2026, 6, 9, 11, 0, tzinfo=TZ)
    ev = _buy(
        entry_ts,
        "PE",
        5,
        strategy="j_plus",
        or_high=100.0,
        or_low=90.0,
    )
    bars = [
        {"index": 1, "close": 101.0},
        {"index": 2, "close": 102.0},
        {"index": 3, "close": 103.0},
    ]
    streak = max_consecutive_bars_outside_or(bars, 5, 100.0, 90.0, "PE")
    assert streak == 3
    j = {(ev.event_type, ev.side, ev.timestamp): streak}
    assert _entry_blocked(ev, time_gate=None, max_bars_outside_or=2, j_streaks=j, cfg=DEFAULT_CONFIG)
    assert not _entry_blocked(ev, time_gate=None, max_bars_outside_or=3, j_streaks=j, cfg=DEFAULT_CONFIG)


def test_filter_drops_orphan_exits():
    entry = datetime(2026, 6, 9, 14, 35, tzinfo=TZ)
    exit_ts = datetime(2026, 6, 9, 15, 0, tzinfo=TZ)
    events = [
        _buy(entry, "CE", 50),
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
    filtered = filter_trade_events(events, time_gate="13:30", max_bars_outside_or=None, j_streaks={}, cfg=DEFAULT_CONFIG)
    assert filtered == []


def test_filter_keeps_valid_round_trip():
    entry = datetime(2026, 6, 9, 10, 0, tzinfo=TZ)
    exit_ts = datetime(2026, 6, 9, 10, 30, tzinfo=TZ)
    events = [
        _buy(entry, "CE", 10),
        SignalEvent(
            timestamp=exit_ts,
            event_type="TARGET_CE",
            direction="LONG_EXIT",
            side="CE",
            price=120.0,
            reason="TARGET_HIT",
            bar_index=16,
        ),
    ]
    filtered = filter_trade_events(events, time_gate=None, max_bars_outside_or=None, j_streaks={}, cfg=DEFAULT_CONFIG)
    trades = pair_trades(filtered)
    assert len(trades) == 1
    assert trades[0].pnl_pts == 20.0


def test_run_sweep_matrix_empty_replay():
    import pandas as pd

    logger = ReplayLogger()
    df = pd.DataFrame(columns=["session_date", "timestamp", "open", "high", "low", "close", "volume"])
    cells = run_sweep_matrix(logger, df, 0)
    assert len(cells) == 16
    assert cells[0].total_trades == 0
