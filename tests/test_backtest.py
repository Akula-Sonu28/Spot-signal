"""Tests for backtest trade pairing and summary."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.backtest import pair_trades, run_backtest, summarize_trades
from bot.logger import ReplayLogger, SignalEvent

TZ = ZoneInfo("Asia/Kolkata")


def _evt(et: str, side: str, price: float, hour: int, minute: int, **extra) -> SignalEvent:
    return SignalEvent(
        timestamp=datetime(2026, 6, 9, hour, minute, tzinfo=TZ),
        event_type=et,
        direction="LONG" if side == "CE" else "SHORT",
        side=side,
        price=price,
        stop=price - 30 if side == "CE" else price + 30,
        target=price + 54 if side == "CE" else price - 54,
        reason=extra.get("reason", ""),
        bar_index=0,
        extra=extra,
    )


def test_pair_ce_target_win():
    events = [
        _evt("BUY_CE", "CE", 100.0, 10, 0),
        _evt("TARGET_CE", "CE", 154.0, 11, 0, reason="TARGET_HIT"),
    ]
    trades = pair_trades(events)
    assert len(trades) == 1
    assert trades[0].pnl_pts == 54.0
    assert trades[0].r_multiple == 1.8


def test_pair_pe_sl_loss():
    events = [
        _evt("BUY_PE", "PE", 200.0, 9, 45),
        _evt("SL_PE", "PE", 230.0, 10, 15, reason="STOP_HIT"),
    ]
    trades = pair_trades(events)
    assert len(trades) == 1
    assert trades[0].pnl_pts == -30.0
    assert trades[0].r_multiple == -1.0


def test_run_backtest_from_logger():
    logger = ReplayLogger()
    logger.events = [
        _evt("BUY_CE", "CE", 100.0, 10, 0),
        _evt("TARGET_CE", "CE", 154.0, 11, 0, reason="TARGET_HIT"),
        _evt("BUY_PE", "PE", 200.0, 14, 0),
        _evt("TARGET_PE", "PE", 146.0, 15, 0, reason="TARGET_HIT"),
    ]
    summary = run_backtest(logger, session_dates=["2026-06-09"])
    assert summary.total_trades == 2
    assert summary.wins == 2
    assert summary.total_pnl_pts == 108.0


def test_summarize_profit_factor():
    trades = pair_trades(
        [
            _evt("BUY_CE", "CE", 100.0, 10, 0),
            _evt("TARGET_CE", "CE", 154.0, 11, 0),
            _evt("BUY_PE", "PE", 200.0, 14, 0),
            _evt("SL_PE", "PE", 230.0, 15, 0),
        ]
    )
    s = summarize_trades(trades, sessions=1)
    assert s.wins == 1 and s.losses == 1
    assert s.profit_factor == 54.0 / 30.0
