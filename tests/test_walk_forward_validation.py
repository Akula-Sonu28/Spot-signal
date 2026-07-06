"""Tests for walk-forward validation (Workstream B)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.logger import SignalEvent
from scripts.walk_forward_validation import (
    MIN_OOS_J_PLUS_TRADES,
    OVERFIT_PF_RATIO,
    WalkForwardReport,
    count_j_plus_trades,
    format_walk_forward_table,
)

TZ = ZoneInfo("Asia/Kolkata")


def _buy_j(ts: datetime, side: str) -> SignalEvent:
    return SignalEvent(
        timestamp=ts,
        event_type=f"BUY_{side}",
        direction="LONG" if side == "CE" else "SHORT",
        side=side,
        price=100.0,
        stop=90.0,
        target=120.0,
        bar_index=1,
        extra={"strategy": "j_plus"},
    )


def test_count_j_plus_trades():
    entry = datetime(2026, 6, 9, 10, 0, tzinfo=TZ)
    exit_t = datetime(2026, 6, 9, 11, 0, tzinfo=TZ)
    events = [
        _buy_j(entry, "CE"),
        SignalEvent(
            timestamp=exit_t,
            event_type="TARGET_CE",
            direction="LONG_EXIT",
            side="CE",
            price=120.0,
            bar_index=2,
        ),
        SignalEvent(
            timestamp=datetime(2026, 6, 9, 14, 0, tzinfo=TZ),
            event_type="BUY_PE",
            direction="SHORT",
            side="PE",
            price=100.0,
            bar_index=3,
            extra={"strategy": "v38"},
        ),
    ]
    assert count_j_plus_trades(events) == 1


def test_overfit_threshold_constant():
    assert OVERFIT_PF_RATIO == 1.5
    assert MIN_OOS_J_PLUS_TRADES == 15


def test_format_walk_forward_table():
    from scripts.research_sweeps import SweepCell

    def _row(horizon: str, variant: str, pf: float, j: int = 0):
        from scripts.walk_forward_validation import HorizonResult

        return HorizonResult(
            horizon,
            variant,
            SweepCell(None, None, 10, 60.0, 100.0, pf, 50.0),
            j_plus_trades=j,
        )

    report = WalkForwardReport(
        is_baseline=_row("IS (Train)", "Baseline v3.9", 1.5),
        is_candidate=_row("IS (Train)", "13:30 + Cap 2", 2.0),
        oos_baseline=_row("OOS (Test)", "Baseline v3.9", 1.4),
        oos_candidate=_row("OOS (Test)", "13:30 + Cap 2", 1.0, j=16),
        alpha_degradation_ratio=0.5,
        overfit_warning=True,
        overfit_note="OVERFIT WARNING",
        oos_j_plus_significant=True,
    )
    text = format_walk_forward_table(report)
    assert "Walk-Forward Performance Matrix" in text
    assert "13:30 + Cap 2" in text
    assert "PASS" in text
