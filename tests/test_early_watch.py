"""Tests for early OR-break watch alerts."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.alerts import TelegramAlerter
from bot.config import AppConfig, StrategyConfig
from bot.data_feed import FeedSnapshot, is_bar_complete
from bot.early_watch import detect_early_or_watch, early_watch_key
from bot.scheduler import LiveScheduler
from bot.state import LiveMonitorState, Position, make_day_state

TZ = ZoneInfo("Asia/Kolkata")


def _v38_day():
    day = make_day_state("2026-06-15")
    day.or_high = 24011.40
    day.or_low = 23934.10
    day.or_defined = True
    day.day_mode = "V38"
    return day


def test_detect_watch_pe_on_forming_breakdown():
    day = _v38_day()
    cfg = StrategyConfig()
    watch = detect_early_or_watch(
        day,
        Position(),
        spot=23931.80,
        vwap=23933.05,
        adx=47.9,
        cfg=cfg,
    )
    assert watch == "WATCH_PE"


def test_no_watch_when_close_still_inside_or():
    day = _v38_day()
    cfg = StrategyConfig()
    watch = detect_early_or_watch(
        day,
        Position(),
        spot=23935.45,
        vwap=23946.17,
        adx=47.9,
        cfg=cfg,
    )
    assert watch is None


def test_no_watch_on_j_plus_day():
    day = _v38_day()
    day.or_high = 24150.0
    day.or_low = 24000.0
    day.day_mode = "J_PLUS"
    cfg = StrategyConfig()
    watch = detect_early_or_watch(
        day,
        Position(),
        spot=23990.0,
        vwap=23980.0,
        adx=25.0,
        cfg=cfg,
    )
    assert watch is None


def test_early_watch_key_dedupes_per_bar():
    bar_open = datetime(2026, 6, 15, 12, 10, tzinfo=TZ)
    key = early_watch_key("2026-06-15", "WATCH_PE", bar_open)
    assert key == "2026-06-15:WATCH_PE:2026-06-15T12:10:00+05:30"


def test_telegram_watch_message_shows_bar_close_time():
    text: list[str] = []
    alerter = TelegramAlerter(AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="x",
    ))
    alerter.send = lambda msg, **_: text.append(msg)  # type: ignore[method-assign]

    alerter.or_break_pending(
        "WATCH_PE",
        spot=23931.80,
        or_high=24011.40,
        or_low=23934.10,
        bar_open=datetime(2026, 6, 15, 12, 10, tzinfo=TZ),
        now=datetime(2026, 6, 15, 12, 12, tzinfo=TZ),
        vwap=23933.05,
        adx=47.9,
    )
    assert "WATCH_PE" in text[0]
    assert "12:12 IST" in text[0]
    assert "12:15 IST" in text[0]
    assert "below VWAP" in text[0]
    assert "NOT an entry" in text[0]


def test_scheduler_sends_watch_once_on_forming_bar():
    sent: list[str] = []

    class FakeAlerter:
        def or_break_pending(self, *args, **kwargs) -> None:
            sent.append("watch")

    class FakeLog:
        def log_system(self, event_type: str, message: str) -> None:
            pass

    cfg = AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x")
    scheduler = LiveScheduler(cfg, FakeAlerter(), FakeLog())  # type: ignore[arg-type]
    monitor = LiveMonitorState(session_date="2026-06-15", day=_v38_day())

    bar_open = datetime(2026, 6, 15, 12, 10, tzinfo=TZ)
    now = datetime(2026, 6, 15, 12, 12, tzinfo=TZ)
    df = pd.DataFrame([{
        "timestamp": bar_open,
        "open": 23935.75,
        "high": 23940.8,
        "low": 23930.05,
        "close": 23931.80,
        "volume": 0.0,
        "vwap": 23933.05,
        "adx": 47.9,
        "session_date": "2026-06-15",
    }])
    snapshot = FeedSnapshot(
        futures_key="NSE_FO|1",
        dataframe=df,
        fetched_at=now,
        today_bar_count=1,
    )

    scheduler._maybe_early_or_watch(monitor, snapshot, now, "2026-06-15", catch_up_mode=False)
    scheduler._maybe_early_or_watch(monitor, snapshot, now, "2026-06-15", catch_up_mode=False)

    assert sent == ["watch"]
    assert len(monitor.dispatched_early_watch_keys) == 1


def test_forming_bar_not_complete_until_after_close_buffer():
    bar_open = datetime(2026, 6, 15, 12, 10, tzinfo=TZ)
    assert not is_bar_complete(bar_open, datetime(2026, 6, 15, 12, 14, tzinfo=TZ), buffer_sec=15)
    assert is_bar_complete(bar_open, datetime(2026, 6, 15, 12, 15, 15, tzinfo=TZ), buffer_sec=15)
