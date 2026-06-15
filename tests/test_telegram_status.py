"""Tests for Telegram status queries and formatters."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

from bot.config import AppConfig, CombinedStrategyConfig
from bot.state import LiveMonitorState, Position, PositionSide, make_day_state
from bot.strategy_j import J_TRAP_ROBUST
from bot.telegram_commands import parse_command
from bot.telegram_status import (
    TELEGRAM_MAX_CHARS,
    FeedProbeResult,
    TodayContext,
    count_errors_today,
    filter_trade_events,
    format_health,
    format_levels,
    format_next,
    format_or,
    format_ping,
    format_position,
    format_today,
    next_bar_close,
    probe_market_feed,
    read_today_events,
    truncate_telegram,
)

IST = ZoneInfo("Asia/Kolkata")


def _cfg() -> AppConfig:
    return AppConfig(
        upstox_access_token="token",
        telegram_bot_token="x",
        telegram_chat_id="12345",
    )


def _ctx(
    *,
    session_date: str = "2026-06-15",
    hour: int = 12,
    minute: int = 12,
    monitor: LiveMonitorState | None = None,
    market_pid: int | None = 4242,
) -> TodayContext:
    now = datetime(2026, 6, 15, hour, minute, tzinfo=IST)
    return TodayContext(
        now=now,
        session_date=session_date,
        monitor=monitor,
        remote_pid=1111,
        market_pid=market_pid,
    )


def _day_monitor(
    *,
    mode: str = "V38",
    or_high: float = 24000.0,
    or_low: float = 23920.0,
    or_defined: bool = True,
) -> LiveMonitorState:
    day = make_day_state("2026-06-15")
    day.or_defined = or_defined
    day.or_high = or_high
    day.or_low = or_low
    day.day_mode = mode
    return LiveMonitorState(session_date="2026-06-15", day=day)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["timestamp", "event_type", "price"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_parse_command_pos_alias():
    assert parse_command("/pos") is not None
    assert parse_command("/pos").name == "position"


def test_read_today_events_filters_by_date(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    _write_csv(csv_path, [
        {"timestamp": "2026-06-15T12:10:00+05:30", "event_type": "BUY_PE", "price": "23931.8"},
        {"timestamp": "2026-06-14T12:10:00+05:30", "event_type": "BUY_CE", "price": "24000"},
        {"timestamp": "2026-06-15T13:40:00+05:30", "event_type": "TARGET_PE", "price": "23850"},
    ])
    rows = read_today_events(csv_path, "2026-06-15")
    assert len(rows) == 2
    assert all(r["timestamp"].startswith("2026-06-15") for r in rows)


def test_filter_trade_events_and_error_count(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    _write_csv(csv_path, [
        {"timestamp": "2026-06-15T09:00:00+05:30", "event_type": "DATA_ERROR", "price": ""},
        {"timestamp": "2026-06-15T12:10:00+05:30", "event_type": "BUY_PE", "price": "23931"},
        {"timestamp": "2026-06-15T12:05:00+05:30", "event_type": "WATCH_PE", "price": ""},
        {"timestamp": "2026-06-15T09:01:00+05:30", "event_type": "OR_READY", "price": ""},
    ])
    rows = read_today_events(csv_path, "2026-06-15")
    trades = filter_trade_events(rows)
    assert len(trades) == 2
    assert count_errors_today(csv_path, "2026-06-15") == 1


def test_format_ping():
    text = format_ping(_ctx())
    assert "Pong" in text
    assert "12:12 IST" in text


def test_format_or_v38_mode():
    monitor = _day_monitor(mode="V38")
    text = format_or(_ctx(monitor=monitor), _cfg())
    assert "BUY CE" in text
    assert "BUY PE" in text
    assert "24000.00" in text


def test_format_or_j_plus_mode():
    cfg = AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="x",
        combined=CombinedStrategyConfig(
            strategy=AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x").strategy,
            enable_j_plus=True,
            j_trap=J_TRAP_ROBUST,
        ),
    )
    monitor = _day_monitor(mode="J_PLUS", or_high=150.0, or_low=40.0)
    text = format_or(_ctx(monitor=monitor), cfg)
    assert "J+ trap-fade" in text
    assert "v3.8 breakout module is OFF" in text


def test_format_or_skip_mode():
    monitor = _day_monitor(mode="SKIP")
    text = format_or(_ctx(monitor=monitor), _cfg())
    assert "No trades today" in text


def test_format_position_flat_and_open():
    flat = format_position(_ctx(monitor=_day_monitor()), _cfg())
    assert flat == "Position: FLAT"

    monitor = _day_monitor()
    monitor.position = Position(
        side=PositionSide.PE,
        entry_price=23931.8,
        stop=23980.0,
        target=23850.0,
        option_symbol="NIFTY25JUN23900PE",
    )
    probe = FeedProbeResult(ok=True, last_close=23900.0)
    text = format_position(_ctx(monitor=monitor), _cfg(), probe)
    assert "PE" in text
    assert "23931.80" in text
    assert "P&L" in text


def test_format_today_empty_and_populated(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    empty = format_today(_ctx(), _cfg(), csv_path)
    assert "No trade signals" in empty

    _write_csv(csv_path, [
        {"timestamp": "2026-06-15T12:10:00+05:30", "event_type": "BUY_PE", "price": "23931.8"},
    ])
    populated = format_today(_ctx(), _cfg(), csv_path)
    assert "BUY_PE" in populated
    assert "Trade events: 1" in populated


def test_format_levels_above_and_below_or():
    monitor = _day_monitor(or_high=24000.0, or_low=23920.0)
    probe_above = FeedProbeResult(ok=True, last_close=24010.0)
    above = format_levels(_ctx(monitor=monitor), _cfg(), probe_above)
    assert "above OR High" in above

    probe_below = FeedProbeResult(ok=True, last_close=23910.0)
    below = format_levels(_ctx(monitor=monitor), _cfg(), probe_below)
    assert "below trigger" in below


def test_format_next_bar_close_at_1212():
    monitor = _day_monitor()
    text = format_next(_ctx(hour=12, minute=12, monitor=monitor), _cfg())
    assert "12:15 IST" in text
    close = next_bar_close(datetime(2026, 6, 15, 12, 12, tzinfo=IST), _cfg().strategy)
    assert close is not None
    assert close.hour == 12 and close.minute == 15


def test_format_health_bot_down_and_errors(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    _write_csv(csv_path, [
        {"timestamp": "2026-06-15T09:00:00+05:30", "event_type": "DATA_STALE", "price": ""},
        {"timestamp": "2026-06-15T12:10:00+05:30", "event_type": "BUY_PE", "price": "23931"},
    ])
    ctx = _ctx(market_pid=None)
    probe = FeedProbeResult(ok=False, skipped=True, skip_reason="market closed — feed probe skipped")
    text = format_health(ctx, _cfg(), probe, csv_path)
    assert "STOPPED" in text
    assert "Feed errors today: 1" in text
    assert "1 entry" in text


def test_probe_market_feed_skipped_off_hours():
    now = datetime(2026, 6, 15, 20, 0, tzinfo=IST)
    result = probe_market_feed(_cfg(), now)
    assert result.skipped
    assert "skipped" in result.skip_reason


def test_probe_market_feed_failure(monkeypatch):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=IST)

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("bot.data_feed.fetch_with_retry", boom)
    result = probe_market_feed(_cfg(), now)
    assert not result.ok
    assert "network down" in (result.error or "")


def test_truncate_telegram():
    long_text = "x" * (TELEGRAM_MAX_CHARS + 100)
    out = truncate_telegram(long_text)
    assert len(out) <= TELEGRAM_MAX_CHARS
    assert out.endswith("(truncated)")
