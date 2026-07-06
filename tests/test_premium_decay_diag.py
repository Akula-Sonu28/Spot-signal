"""Tests for premium decay / chop diagnostics."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import DEFAULT_CONFIG
from bot.logger import ReplayLogger
from bot.premium_decay_diag import (
    DEFAULT_CHOP_ATR_BAND,
    DEFAULT_CHOP_MIN_BARS,
    SPOT_OPTION_GAPS,
    analyze_bar_chop,
    analyze_chop_from_replay,
    in_chop_band,
    log_position_snapshot,
)
from bot.state import Position, PositionSide

TZ = ZoneInfo("Asia/Kolkata")


def _make_df(closes: list[float], atr: float = 10.0) -> pd.DataFrame:
    ts = datetime(2026, 6, 9, 10, 0, tzinfo=TZ)
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "timestamp": ts.replace(minute=10 + i * 5),
            "session_date": "2026-06-09",
            "open": c,
            "high": c + 1,
            "low": c - 1,
            "close": c,
            "volume": 1000.0,
            "atr": atr,
        })
    return pd.DataFrame(rows)


def test_spot_option_gaps_documented():
    assert "wick_risk" in SPOT_OPTION_GAPS
    assert any("close_only_sl" in g.lower() or "CLOSE_ONLY" in g for g in SPOT_OPTION_GAPS["wick_risk"])


def test_in_chop_band_within_atr():
    assert in_chop_band(2.0, 10.0, DEFAULT_CHOP_ATR_BAND)
    assert not in_chop_band(5.0, 10.0, DEFAULT_CHOP_ATR_BAND)


def test_wick_would_sl_close_only():
    pos = Position(
        side=PositionSide.CE,
        entry_price=100.0,
        stop=98.0,
        entry_bar_index=0,
    )
    state = analyze_bar_chop(
        pos,
        bar_index=1,
        timestamp=datetime(2026, 6, 9, 10, 5, tzinfo=TZ),
        close=99.0,
        high=101.0,
        low=97.0,
        atr=10.0,
        cfg=DEFAULT_CONFIG,
    )
    assert state.wick_would_sl is True


def test_log_position_snapshot_flags_chop():
    pos = Position(side=PositionSide.CE, entry_price=100.0, stop=90.0, entry_bar_index=0)
    logger = ReplayLogger()

    class Bar:
        index = 5
        timestamp = datetime(2026, 6, 9, 10, 25, tzinfo=TZ)
        session_date = "2026-06-09"
        close = 100.5
        high = 101.0
        low = 99.5
        atr = 10.0

    event = log_position_snapshot(pos, Bar(), logger, DEFAULT_CONFIG, chop_streak=3)
    assert event.event_type == "POSITION_PREMIUM"
    assert event.extra["in_chop_band"] is True
    assert event.extra["high_theta_decay_risk"] is True


def test_chop_report_on_empty_trades():
    df = _make_df([100.0] * 8)
    logger = ReplayLogger()
    summary = analyze_chop_from_replay(logger, df, DEFAULT_CONFIG)
    assert summary.total_trades == 0


def test_position_premium_emitted_during_open_trade():
    from bot.combined import process_session_bar
    from bot.config import load_combined_config
    from bot.state import ReplayState, make_day_state
    from bot.strategy import build_bar_context

    cfg = DEFAULT_CONFIG
    combined = load_combined_config(cfg)
    state = ReplayState(day=make_day_state("2026-06-09"))
    state.day.or_high = 100.0
    state.day.or_low = 90.0
    state.day.or_defined = True
    state.day.day_mode = "V38"
    state.position = Position(
        side=PositionSide.CE,
        entry_price=101.0,
        stop=89.0,
        target=120.0,
        entry_bar_index=0,
        entry_time=datetime(2026, 6, 9, 10, 0, tzinfo=TZ),
    )
    logger = ReplayLogger()
    bar = build_bar_context(
        index=1,
        timestamp=datetime(2026, 6, 9, 10, 5, tzinfo=TZ),
        session_date="2026-06-09",
        o=100.5,
        h=101.5,
        l=100.0,
        c=100.5,
        vol=1000,
        vwap=100.0,
        atr=10.0,
        adx=25.0,
        cfg=cfg,
        tz=TZ,
    )
    process_session_bar(state, bar, logger, combined)
    prem = [e for e in logger.events if e.event_type == "POSITION_PREMIUM"]
    assert len(prem) == 1
    assert prem[0].extra["bars_in_trade"] == 1
