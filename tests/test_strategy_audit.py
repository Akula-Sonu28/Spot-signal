"""Tests for bot/strategy_audit.py structural gap analyses."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import DEFAULT_CONFIG
from bot.logger import ReplayLogger, SignalEvent
from bot.backtest import TradeRecord, pair_trades
from bot.strategy_audit import (
    audit_adx_lag_trap,
    audit_fixed_buffer_stopout,
    audit_j_acceptance,
    audit_playbook_cliff,
    audit_time_buckets,
    build_session_or_widths,
    format_strategy_audit_report,
    run_strategy_audit,
    _bars_outside_or_before_entry,
    _time_bucket_label,
)

TZ = ZoneInfo("Asia/Kolkata")


def _trade(side: str, pnl: float, session: str = "2026-06-09", hour: int = 10) -> TradeRecord:
    entry = datetime(2026, 6, 9, hour, 0, tzinfo=TZ)
    exit_t = datetime(2026, 6, 9, hour, 30, tzinfo=TZ)
    ep = 100.0
    return TradeRecord(
        session_date=session,
        side=side,
        entry_time=entry,
        exit_time=exit_t,
        entry_price=ep,
        exit_price=ep + pnl if side == "CE" else ep - pnl,
        stop=ep - 30 if side == "CE" else ep + 30,
        target=ep + 54 if side == "CE" else ep - 54,
        exit_reason="STOP_HIT" if pnl < 0 else "TARGET_HIT",
        pnl_pts=pnl,
        risk_pts=30.0,
        r_multiple=pnl / 30.0,
    )


def test_playbook_cliff_splits_zones():
    widths = {"2026-06-01": 92.0, "2026-06-02": 108.0}
    trades = [
        _trade("CE", 20.0, "2026-06-01"),
        _trade("PE", -30.0, "2026-06-02"),
    ]
    cliff = audit_playbook_cliff(trades, widths)
    assert cliff.zone_85_100.trades == 1
    assert cliff.zone_100_115.trades == 1
    assert cliff.boundary_sessions == 2


def test_time_bucket_morning():
    ts = datetime(2026, 6, 9, 9, 35, tzinfo=TZ)  # bar open → close 09:40
    assert _time_bucket_label(ts, DEFAULT_CONFIG) == "morning"


def test_adx_lag_trap_tags_flat_post_entry():
    entry_ts = datetime(2026, 6, 9, 10, 0, tzinfo=TZ)
    events = [
        SignalEvent(
            timestamp=entry_ts,
            event_type="BUY_CE",
            direction="LONG",
            side="CE",
            price=100.0,
            bar_index=10,
            extra={"adx": 25.0, "atr": 20.0, "strategy": "v38"},
        ),
        SignalEvent(
            timestamp=datetime(2026, 6, 9, 10, 30, tzinfo=TZ),
            event_type="SL_CE",
            direction="LONG_EXIT",
            side="CE",
            price=70.0,
            reason="STOP_HIT",
        ),
    ]
    trades = pair_trades(events)
    bars = {
        "2026-06-09": [
            {"index": 10, "close": 100.0, "high": 101, "low": 99, "atr": 20, "adx": 25},
            {"index": 11, "close": 100.5, "high": 101, "low": 99.5, "atr": 20, "adx": 24},
            {"index": 12, "close": 100.2, "high": 101, "low": 99.8, "atr": 20, "adx": 23},
            {"index": 13, "close": 100.1, "high": 101, "low": 99.9, "atr": 20, "adx": 22},
        ]
    }
    audit = audit_adx_lag_trap(trades, events, bars)
    assert audit.tagged_trades == 1
    assert audit.tagged_losses == 1


def test_j_bars_outside_or():
    bars = [
        {"index": 0, "close": 101.0},
        {"index": 1, "close": 102.0},
        {"index": 2, "close": 103.0},
        {"index": 3, "close": 99.0},
    ]
    n = _bars_outside_or_before_entry(bars, 3, or_high=100.0, or_low=90.0, side="PE")
    assert n == 3


def test_j_acceptance_flags_long_outside():
    entry_ts = datetime(2026, 6, 9, 11, 0, tzinfo=TZ)
    events = [
        SignalEvent(
            timestamp=entry_ts,
            event_type="BUY_PE",
            direction="SHORT",
            side="PE",
            price=100.0,
            bar_index=5,
            extra={"strategy": "j_plus", "or_high": 100.0, "or_low": 90.0},
        ),
        SignalEvent(
            timestamp=datetime(2026, 6, 9, 11, 30, tzinfo=TZ),
            event_type="SL_PE",
            direction="SHORT_EXIT",
            side="PE",
            price=130.0,
            reason="STOP_HIT",
        ),
    ]
    trades = pair_trades(events)
    session_bars = {
        "2026-06-09": [
            {"index": 1, "close": 101.0},
            {"index": 2, "close": 102.0},
            {"index": 3, "close": 103.0},
            {"index": 4, "close": 101.5},
            {"index": 5, "close": 99.0},
        ]
    }
    audit = audit_j_acceptance(trades, events, session_bars)
    assert audit.value_acceptance_short == 1
    assert audit.acceptance_losses == 1


def test_format_report_runs():
    logger = ReplayLogger()
    df = pd.DataFrame([{
        "timestamp": datetime(2026, 6, 9, 9, 15, tzinfo=TZ),
        "session_date": "2026-06-09",
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1,
    }])
    report = run_strategy_audit(logger, df)
    text = format_strategy_audit_report(report)
    assert "PLAYBOOK CLIFF" in text
    assert "ADX LAG" in text
