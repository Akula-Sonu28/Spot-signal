"""Tests for Phase 1 live monitoring helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.config import AUTO_TRADE, AppConfig, StrategyConfig, validate_app_config
from bot.data_feed import is_bar_complete
from bot.alerts import TelegramAlerter
from bot.logger import SignalEvent
from bot.scheduler import (
    LiveScheduler,
    after_monitor_close,
    alert_dispatch_key,
    in_monitor_window,
    in_signal_window,
    should_send_data_warning,
    should_send_signals_paused_alert,
)
from bot.state import LiveMonitorState, make_day_state

TZ = ZoneInfo("Asia/Kolkata")


def test_auto_trade_hard_disabled():
    assert AUTO_TRADE is False


def test_validate_app_config_missing():
    cfg = AppConfig(
        upstox_access_token="",
        telegram_bot_token="",
        telegram_chat_id="",
    )
    errors = validate_app_config(cfg)
    assert len(errors) == 3


def test_bar_complete_waits_for_buffer():
    bar_open = datetime(2026, 6, 9, 10, 40, tzinfo=TZ)
    assert not is_bar_complete(bar_open, datetime(2026, 6, 9, 10, 44, 59, tzinfo=TZ), buffer_sec=15)
    assert is_bar_complete(bar_open, datetime(2026, 6, 9, 10, 45, 15, tzinfo=TZ), buffer_sec=15)


def test_alert_dispatch_key_stable():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 11, 9, 45, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=23151.75,
        bar_index=231,
    )
    assert alert_dispatch_key(event) == "2026-06-11:BUY_CE:231"


def test_catch_up_trade_events_are_queued_once():
    monitor = LiveMonitorState(session_date="2026-06-11", day=make_day_state("2026-06-11"))
    scheduler = LiveScheduler(AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x"), None, None)  # type: ignore[arg-type]
    event = SignalEvent(
        timestamp=datetime(2026, 6, 11, 9, 45, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=23151.75,
        bar_index=231,
    )
    out: list[SignalEvent] = []
    assert scheduler._queue_trade_alerts(monitor, [event], out) == 1
    assert len(out) == 1
    assert scheduler._queue_trade_alerts(monitor, [event], out) == 0
    assert len(out) == 1


def test_telegram_format_buy_ce_catch_up_note():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 11, 9, 45, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=23151.75,
        stop=23113.0,
        target=23221.5,
        reason="ORB_BREAKOUT+VWAP+ADX",
        extra={"catch_up": True, "or_high": 23148.05, "or_low": 23072.05, "adx": 51.0, "vwap": 23124.6},
    )
    text = TelegramAlerter.format_signal(event)
    assert "Catch-up alert" in text
    assert "BUY_CE" in text


def test_telegram_format_buy_ce():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 9, 14, 50, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=23267.25,
        stop=23242.8,
        target=23311.26,
        reason="ORB_BREAKOUT+VWAP+ADX",
        extra={"or_high": 23259.45, "or_low": 23194.9, "adx": 20.1, "vwap": 23172.0},
    )
    text = TelegramAlerter.format_signal(event)
    assert "BUY_CE" in text
    assert "23267.25" in text
    assert "manual execution" in text


def test_monitor_window_helpers():
    cfg = StrategyConfig()
    assert in_monitor_window(datetime(2026, 6, 11, 9, 15, tzinfo=TZ), cfg)
    assert not in_monitor_window(datetime(2026, 6, 11, 9, 14, tzinfo=TZ), cfg)
    assert after_monitor_close(datetime(2026, 6, 11, 15, 31, tzinfo=TZ), cfg)
    assert not after_monitor_close(datetime(2026, 6, 11, 15, 30, tzinfo=TZ), cfg)


def test_in_signal_window():
    cfg = StrategyConfig()
    assert not in_signal_window(datetime(2026, 6, 11, 9, 29, tzinfo=TZ), cfg)
    assert in_signal_window(datetime(2026, 6, 11, 9, 30, tzinfo=TZ), cfg)
    assert in_signal_window(datetime(2026, 6, 11, 12, 0, tzinfo=TZ), cfg)
    assert not in_signal_window(datetime(2026, 6, 11, 15, 15, tzinfo=TZ), cfg)


def test_should_send_data_warning_throttle():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=TZ)
    last = datetime(2026, 6, 11, 9, 50, tzinfo=TZ)
    assert not should_send_data_warning(last, now, throttle_minutes=15)
    assert should_send_data_warning(None, now)
    assert should_send_data_warning(
        datetime(2026, 6, 11, 9, 44, tzinfo=TZ),
        now,
        throttle_minutes=15,
    )


def test_should_send_signals_paused_alert():
    now = datetime(2026, 6, 11, 9, 50, tzinfo=TZ)
    seen = datetime(2026, 6, 11, 9, 30, tzinfo=TZ)
    assert should_send_signals_paused_alert(seen, None, now, False)
    assert not should_send_signals_paused_alert(seen, None, now, True)
    assert not should_send_signals_paused_alert(seen, "2026-06-11T09:15:00+05:30", now, False)
    assert not should_send_signals_paused_alert(
        datetime(2026, 6, 11, 9, 40, tzinfo=TZ),
        None,
        now,
        False,
        pause_minutes=15,
    )


def test_maybe_alert_data_issue_only_during_signal_hours():
    monitor = LiveMonitorState(session_date="2026-06-11", day=make_day_state("2026-06-11"))
    sent: list[str] = []

    class FakeAlerter:
        def data_error(self, message: str, *, during_signal_hours: bool = False) -> None:
            sent.append(message)

    cfg = AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x")
    scheduler = LiveScheduler(cfg, FakeAlerter(), None)  # type: ignore[arg-type]

    scheduler._maybe_alert_data_issue(monitor, "stale", datetime(2026, 6, 11, 9, 20, tzinfo=TZ))
    assert sent == []

    scheduler._maybe_alert_data_issue(monitor, "stale", datetime(2026, 6, 11, 10, 0, tzinfo=TZ))
    assert len(sent) == 1
    scheduler._maybe_alert_data_issue(monitor, "stale again", datetime(2026, 6, 11, 10, 5, tzinfo=TZ))
    assert len(sent) == 1


def test_track_signals_paused_sends_once():
    monitor = LiveMonitorState(session_date="2026-06-11", day=make_day_state("2026-06-11"))
    paused: list[int] = []

    class FakeAlerter:
        def signals_paused(self, minutes_without_bars: int) -> None:
            paused.append(minutes_without_bars)

    class FakeLog:
        def log_system(self, event_type: str, message: str) -> None:
            pass

    cfg = AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x")
    scheduler = LiveScheduler(cfg, FakeAlerter(), FakeLog())  # type: ignore[arg-type]
    now = datetime(2026, 6, 11, 9, 50, tzinfo=TZ)
    monitor.entry_window_seen_without_bars = datetime(2026, 6, 11, 9, 30, tzinfo=TZ).isoformat()

    scheduler._track_signals_paused(monitor, now, 0)
    assert paused == [15]
    scheduler._track_signals_paused(monitor, now, 0)
    assert paused == [15]


def test_auto_trade_env_rejected(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE", "true")
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "x")
    with pytest.raises(RuntimeError, match="AUTO_TRADE"):
        from bot.config import load_app_config

        load_app_config(env_file="/nonexistent/.env")
