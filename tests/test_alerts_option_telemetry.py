"""Workstream E — option microstructure alert annotations (mock, no network)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter
from bot.logger import SignalEvent
from bot.state import Position, PositionSide
from bot.telegram_format import html_to_plain

TZ = ZoneInfo("Asia/Kolkata")


def _buy_ce_event(**extra) -> SignalEvent:
    base = {
        "or_high": 24150.0,
        "or_low": 24100.0,
        "adx": 22.0,
        "vwap": 24140.0,
        "option_strike": 24150,
        "option_type": "CE",
        "option_expiry": "2026-06-26",
        "option_symbol": "NIFTY 24150 CE 26 JUN 26",
        "option_ask": 104.0,
        "option_bid": 100.0,
        "option_spread": 4.0,
    }
    base.update(extra)
    return SignalEvent(
        timestamp=datetime(2026, 6, 15, 10, 0, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=24165.0,
        stop=24120.0,
        target=24220.0,
        reason="ORB_BREAKOUT+VWAP+ADX",
        extra=base,
    )


def test_entry_high_spread_friction_warning():
    event = _buy_ce_event(spread_drag_pct=4.2, entry_slippage_pts=1.8)
    text = TelegramAlerter.format_signal(event)
    assert "HIGH SPREAD FRICTION" in text
    assert "4.2% Drag" in text
    assert "Est. Entry Slippage" in text
    assert "1.8 Premium Pts" in text


def test_entry_no_friction_below_threshold():
    event = _buy_ce_event(spread_drag_pct=2.5, entry_slippage_pts=0.5)
    text = TelegramAlerter.format_signal(event)
    assert "HIGH SPREAD FRICTION" not in text
    assert "Est. Entry Slippage" not in text


def test_exit_target_wick_postmortem():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 12, 35, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=24220.0,
        stop=24120.0,
        target=24220.0,
        reason="TARGET_HIT",
        extra={
            "entry": 24165.0,
            "target_reverted_mid_bar": True,
            "target_held_on_close": False,
        },
    )
    text = TelegramAlerter.format_signal(event)
    assert "mid-bar wick hit only" in text
    assert "low probability execution" in text


def test_exit_target_no_wick_note_when_held():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 12, 35, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=24220.0,
        stop=24120.0,
        target=24220.0,
        reason="TARGET_HIT",
        extra={"entry": 24165.0, "target_reverted_mid_bar": False},
    )
    text = TelegramAlerter.format_signal(event)
    assert "mid-bar wick hit only" not in text


def test_position_status_iv_crush_alarm():
    pos = Position(
        side=PositionSide.CE,
        entry_price=24165.0,
        stop=24120.0,
        target=24220.0,
        iv_at_entry=0.20,
        iv_crush_bleed=True,
        last_iv_delta=-0.03,
    )
    text = TelegramAlerter.format_position_status(pos, spot=24166.0, spot_pnl=1.0)
    assert "IV CRUSH ALARM" in text
    assert "15.0%" in text
    assert "consolidating sideways" in text


def test_position_premium_event_iv_alarm():
    pos = Position(
        side=PositionSide.CE,
        entry_price=24165.0,
        iv_at_entry=0.18,
        iv_crush_bleed=True,
        last_iv_delta=-0.025,
    )
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 11, 30, tzinfo=TZ),
        event_type="POSITION_PREMIUM",
        direction="LONG",
        side="CE",
        price=24166.0,
        reason="PREMIUM_SNAPSHOT",
        extra={"iv_crush_bleed": True, "iv_at_entry": 0.18, "iv_delta": -0.025},
    )
    text = TelegramAlerter.format_signal(event, position=pos)
    assert "POSITION STATUS" in text
    assert "IV CRUSH ALARM" in text


def test_alert_layout_samples_visualization(capsys):
    """Deliverable: example layouts (run `pytest -s tests/test_alerts_option_telemetry.py::test_alert_layout_samples_visualization`)."""
    entry = html_to_plain(
        TelegramAlerter.format_signal(
            _buy_ce_event(spread_drag_pct=4.5, entry_slippage_pts=2.2),
        )
    )
    pos = Position(
        side=PositionSide.CE,
        entry_price=24165.0,
        stop=24120.0,
        target=24220.0,
        option_symbol="NIFTY 24150 CE 26 JUN 26",
        iv_at_entry=0.20,
        iv_crush_bleed=True,
        last_iv_delta=-0.03,
    )
    status = TelegramAlerter.format_position_status(pos, spot=24166.2, spot_pnl=1.2)

    print("\n## Enriched BUY_CE (high spread friction)\n")
    print(entry)
    print("\n## POSITION_STATUS (IV crush alarm)\n")
    print(status)

    assert "HIGH SPREAD FRICTION" in entry
    assert "IV CRUSH ALARM" in status
