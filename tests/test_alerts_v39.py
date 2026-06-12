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
    assert "Module: J+" in text
