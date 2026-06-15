"""Telegram alert text for v3.9 day modes."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter
from bot.config import AppConfig
from bot.logger import SignalEvent


def test_or_ready_j_plus_disabled() -> None:
    captured: list[str] = []
    cfg = AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="1",
    )
    alerter = TelegramAlerter(cfg)
    alerter.send = lambda text, **_: captured.append(text)  # type: ignore[method-assign]

    alerter.or_ready(200.0, 80.0, 120.0, day_mode="J_PLUS", enable_j_plus=False)
    assert captured
    assert "J+ disabled" in captured[0]
    assert "No entries" in captured[0]


def test_fmt_entry_j_plus_module_label() -> None:
    event = SignalEvent(
        timestamp=datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
        event_type="BUY_PE",
        direction="SHORT",
        side="PE",
        price=100.0,
        stop=110.0,
        target=80.0,
        reason="OR_FAKE_BREAK_PE",
        extra={"strategy": "j_plus", "or_high": 90.0, "or_low": 0.0, "adx": 22.0, "vwap": 102.0},
    )
    text = TelegramAlerter.format_signal(event)
    assert "J+ trap-fade" in text
    assert "<b>J+</b>" in text
    assert "<b>10:35 IST</b>" in text


def test_fmt_entry_spot_linked_option_exit_plan() -> None:
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 12, 10, tzinfo=ZoneInfo("Asia/Kolkata")),
        event_type="BUY_PE",
        direction="SHORT",
        side="PE",
        price=23931.80,
        stop=24021.40,
        target=23888.15,
        reason="ORB_BREAKDOWN+VWAP+ADX",
        extra={
            "or_high": 24011.40,
            "or_low": 23934.10,
            "option_strike": 23900,
            "option_type": "PE",
            "option_expiry": "2026-06-26",
            "option_symbol": "NIFTY 23900 PE 26 JUN 26",
            "option_ask": 142.50,
            "option_bid": 141.00,
            "option_spread": 1.50,
            "option_delta": -0.45,
            "option_theta": -8.2,
            "option_iv": 0.185,
            "option_gamma": 0.0004,
        },
    )
    text = TelegramAlerter.format_signal(event)
    assert "Exit when SPOT hits SL" in text
    assert "24021.40" in text
    assert "23888.15" in text
    assert "P&amp;L IF SPOT LEVELS HIT" in text
    assert "+43.6 pts" in text
    assert "δ=-0.45" in text
    assert "θ=-8.2/day" in text
    assert "δ+θ+½γΔS²" in text
    assert "<b>Backup</b>" in text
    assert "₹71.25" in text
    assert "OPTION RISK" not in text
    assert "2R on premium" not in text
    assert "Premium Tgt" not in text
    assert "<b>" in text


def test_html_escape_in_reason() -> None:
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 12, 10, tzinfo=ZoneInfo("Asia/Kolkata")),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=100.0,
        stop=95.0,
        target=110.0,
        reason="TEST<A&B>",
        extra={"or_high": 105.0, "or_low": 95.0},
    )
    text = TelegramAlerter.format_signal(event)
    assert "TEST&lt;A&amp;B&gt;" in text
    assert "TEST<A&B>" not in text


def test_fmt_exit_no_premium_price_targets() -> None:
    event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 13, 35, tzinfo=ZoneInfo("Asia/Kolkata")),
        event_type="TARGET_PE",
        direction="SHORT_EXIT",
        side="PE",
        price=23888.15,
        stop=24021.4,
        target=23888.15,
        reason="TARGET_HIT",
        extra={
            "entry": 23931.8,
            "option_ask": 142.50,
            "option_strike": 23900,
            "option_type": "PE",
            "option_expiry": "2026-06-26",
            "option_symbol": "NIFTY 23900 PE 26 JUN 26",
        },
    )
    text = TelegramAlerter.format_signal(event)
    assert "TARGET HIT" in text
    assert "<b>13:40 IST</b>" in text
    assert "Exit triggered by SPOT" in text
    assert "SL level was" not in text
    assert "Target was" not in text


def test_or_ready_v38_mentions_watch_alerts() -> None:
    captured: list[str] = []
    cfg = AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="1",
    )
    alerter = TelegramAlerter(cfg)
    alerter.send = lambda text, **_: captured.append(text)  # type: ignore[method-assign]

    alerter.or_ready(24011.4, 23934.1, 77.0, day_mode="V38")
    assert "WATCH_CE/PE" in captured[0]
    assert "BUY_CE/PE" in captured[0]
