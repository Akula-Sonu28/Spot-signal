"""Tests for option truth layer telemetry (Workstream C)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from bot.config import AppConfig
from bot.logger import LiveEventLogger, SignalEvent
from bot.option_lookup import OptionQuote
from bot.option_truth import (
    audit_target_wick_close,
    build_entry_telemetry,
    entry_slippage_premium_pts,
    iv_crush_bleed,
    spread_drag_pct,
)
from bot.signal_enrich import enrich_entry_slippage, enrich_target_wick_audit
from bot.state import Position

TZ = ZoneInfo("Asia/Kolkata")


def test_spread_drag_pct():
    assert spread_drag_pct(100.0, 97.0) == 3.0
    assert spread_drag_pct(None, 97.0) is None


def test_entry_slippage_premium_pts_ce():
    # δ=0.5, spot +10 pts → ~5 premium pts slippage
    slip = entry_slippage_premium_pts(
        signal_bar_close=100.0,
        alert_spot=110.0,
        alert_ask=50.0,
        delta=0.5,
        side="CE",
    )
    assert slip == 5.0


def test_build_entry_telemetry_high_spread():
    tel = build_entry_telemetry(
        signal_bar_close=23100.0,
        alert_spot=23100.0,
        ask=100.0,
        bid=96.0,
        delta=0.45,
        side="CE",
        iv=0.14,
    )
    assert tel["spread_drag_pct"] == 4.0
    assert tel.get("high_spread_drag") is True
    assert tel["entry_slippage_pts"] == 0.0
    assert tel["iv_at_entry"] == 0.14


def test_target_wick_audit_ce_reverted():
    audit = audit_target_wick_close(
        event_type="TARGET_CE",
        target=100.0,
        bar_high=101.0,
        bar_low=99.0,
        bar_close=99.5,
    )
    assert audit["target_held_on_close"] is False
    assert audit["target_reverted_mid_bar"] is True


def test_target_wick_audit_ce_held_on_close():
    audit = audit_target_wick_close(
        event_type="TARGET_CE",
        target=100.0,
        bar_high=101.0,
        bar_low=99.0,
        bar_close=100.5,
    )
    assert audit["target_held_on_close"] is True
    assert audit["target_reverted_mid_bar"] is False


def test_enrich_target_wick_audit_on_event():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 9, 11, 0, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=100.0,
        target=100.0,
        bar_index=10,
    )

    class Bar:
        high = 101.0
        low = 99.0
        close = 99.2

    enrich_target_wick_audit(event, Bar())
    assert event.extra["target_held_on_close"] is False
    assert event.extra["target_reverted_mid_bar"] is True


def test_iv_crush_bleed_flat_spot_iv_drop():
    assert iv_crush_bleed(
        iv_at_entry=0.20,
        iv_current=0.17,
        spot_from_entry=2.0,
        atr=20.0,
    )
    assert not iv_crush_bleed(
        iv_at_entry=0.20,
        iv_current=0.19,
        spot_from_entry=2.0,
        atr=20.0,
    )


def test_live_csv_truth_columns(tmp_path):
    path = tmp_path / "signals.csv"
    logger = LiveEventLogger(path)
    logger.append(
        SignalEvent(
            timestamp=datetime(2026, 6, 9, 10, 0, tzinfo=TZ),
            event_type="BUY_CE",
            direction="LONG",
            side="CE",
            price=23100.0,
            extra={
                "entry_slippage_pts": 0.0,
                "spread_drag_pct": 4.5,
                "iv_at_entry": 0.15,
                "iv_crush_bleed": False,
                "target_held_on_close": True,
            },
        )
    )
    text = path.read_text()
    assert "entry_slippage_pts" in text
    assert "iv_crush_bleed" in text
    assert "spread_drag_pct" in text


@patch("bot.signal_enrich.lookup_for_signal")
def test_enrich_entry_slippage(mock_lookup):
    mock_lookup.return_value = OptionQuote(
        strike=23100,
        option_type="CE",
        expiry="2026-06-12",
        trading_symbol="NIFTY",
        instrument_key="k",
        bid=148.0,
        ask=152.0,
        delta=0.5,
        iv=0.16,
        quote_ok=True,
    )
    event = SignalEvent(
        timestamp=datetime(2026, 6, 9, 10, 0, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=23100.0,
        bar_index=1,
    )

    class Bar:
        close = 23105.0

    q = mock_lookup.return_value
    enrich_entry_slippage(event, q, bar=Bar())
    assert event.extra["alert_ask"] == 152.0
    assert event.extra["entry_slippage_pts"] == 2.5
    assert event.extra["spread_drag_pct"] == round((152 - 148) / 152 * 100, 2)
