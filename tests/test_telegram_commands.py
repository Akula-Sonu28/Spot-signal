"""Tests for Telegram remote command handling."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import AppConfig
from bot.telegram_commands import (
    FORCE_TICK_FLAG,
    ParsedCommand,
    SessionProcessControl,
    TelegramCommandHandler,
    is_authorized,
    parse_command,
)


def _cfg() -> AppConfig:
    return AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="12345",
    )


def test_parse_command():
    assert parse_command("/status") == ParsedCommand("status", ())
    assert parse_command("/start@MyBot") == ParsedCommand("start", ())
    assert parse_command("/pos") == ParsedCommand("position", ())
    assert parse_command("/ping") == ParsedCommand("ping", ())
    assert parse_command("hello") is None
    assert parse_command("/unknown") is None


def test_is_authorized():
    cfg = _cfg()
    assert is_authorized("12345", cfg)
    assert is_authorized(12345, cfg)
    assert not is_authorized("99999", cfg)


def test_session_process_control_messages(tmp_path: Path, monkeypatch):
    import sys

    lock = tmp_path / "market-session.lock"
    if sys.platform == "win32":
        script = tmp_path / "run.ps1"
        script.write_text("exit 0\n", encoding="utf-8")
    else:
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
        script.chmod(0o755)
    log = tmp_path / "market-session.log"
    log.write_text("line1\nline2\n", encoding="utf-8")

    proc = SessionProcessControl(lock_file=lock, start_script=script, session_log=log)
    proc.project_root = tmp_path
    monkeypatch.setattr("bot.telegram_commands.time.sleep", lambda _: None)
    states = iter([False, True])
    monkeypatch.setattr(proc, "is_running", lambda: next(states, True))
    msg = proc.start()
    assert "started" in msg.lower() or "pid" in msg.lower()
    monkeypatch.setattr(proc, "is_running", lambda: True)
    assert proc.start().startswith("Already running")
    assert proc.last_log_line() == "line2"
    assert proc.stop() == "Not running."

    lock.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(
        proc,
        "running_pid",
        lambda: 4242,
    )
    monkeypatch.setattr(
        "bot.telegram_commands.terminate_process",
        lambda pid: None,
    )
    assert proc.stop() == "Stop signal sent to pid 4242."


def test_start_rejected_after_market_close(monkeypatch):
    from zoneinfo import ZoneInfo

    sent: list[str] = []

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 11, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    monkeypatch.setattr("bot.telegram_commands.datetime", FixedDatetime)
    handler = TelegramCommandHandler(_cfg(), FakeAlerter())  # type: ignore[arg-type]
    handler._dispatch(ParsedCommand("start", ()))
    assert sent and "closed" in sent[0].lower()


def test_handler_status_and_tick(tmp_path: Path, monkeypatch):
    cfg = _cfg()
    cfg = AppConfig(
        upstox_access_token=cfg.upstox_access_token,
        telegram_bot_token=cfg.telegram_bot_token,
        telegram_chat_id=cfg.telegram_chat_id,
        state_file=tmp_path / "monitor_state.json",
    )
    sent: list[str] = []

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    lock = tmp_path / "lock"
    lock.write_text(f"{os.getpid()}", encoding="utf-8")
    process = SessionProcessControl(lock_file=lock, session_log=tmp_path / "log.txt")
    process.project_root = tmp_path

    monkeypatch.setattr(
        "bot.telegram_commands._project_root",
        lambda: tmp_path,
    )

    from datetime import date

    from bot.config import CombinedStrategyConfig
    from bot.strategy_j import J_TRAP_ROBUST
    from bot.state import LiveMonitorState, make_day_state

    session = date.today().isoformat()
    j_cfg = AppConfig(
        upstox_access_token=cfg.upstox_access_token,
        telegram_bot_token=cfg.telegram_bot_token,
        telegram_chat_id=cfg.telegram_chat_id,
        state_file=cfg.state_file,
        combined=CombinedStrategyConfig(
            strategy=cfg.strategy,
            enable_j_plus=True,
            j_trap=J_TRAP_ROBUST,
        ),
    )
    alerter = FakeAlerter()
    handler = TelegramCommandHandler(j_cfg, alerter, process=process)  # type: ignore[arg-type]
    handler.force_tick_flag = Path("force_tick.request")

    day = make_day_state(session)
    day.or_defined = True
    day.or_high = 150.0
    day.or_low = 40.0
    day.day_mode = "J_PLUS"
    day.trades_today = 0
    monitor = LiveMonitorState(session_date=session, day=day)
    monitor.save(tmp_path / "monitor_state.json")

    handler._dispatch(ParsedCommand("status", ()))
    assert sent and "nifty signal engine" in sent[0].lower()
    assert "Mode:" in sent[0]
    assert "J_PLUS" in sent[0]
    assert "0/1 today" in sent[0]

    sent.clear()
    handler._dispatch(ParsedCommand("tick", ()))
    assert sent and "Tick requested" in sent[0]
    assert (tmp_path / "force_tick.request").exists()

    sent.clear()
    handler._dispatch(ParsedCommand("help", ()))
    assert "/stop" in sent[0]
    assert "/ping" in sent[0]
    assert "/health" in sent[0]


def test_new_command_dispatch(tmp_path: Path, monkeypatch):
    from zoneinfo import ZoneInfo

    cfg = AppConfig(
        upstox_access_token="x",
        telegram_bot_token="x",
        telegram_chat_id="12345",
        state_file=tmp_path / "monitor_state.json",
        event_log_csv=tmp_path / "signals.csv",
    )
    sent: list[str] = []

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    monkeypatch.setattr("bot.telegram_commands._project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bot.telegram_commands.probe_market_feed",
        lambda _cfg, _now: __import__(
            "bot.telegram_status", fromlist=["FeedProbeResult"]
        ).FeedProbeResult(ok=True, last_close=24000.0),
    )

    from bot.state import LiveMonitorState, make_day_state

    day = make_day_state("2026-06-15")
    day.or_defined = True
    day.or_high = 24000.0
    day.or_low = 23920.0
    day.day_mode = "V38"
    monitor = LiveMonitorState(session_date="2026-06-15", day=day)
    monitor.save(tmp_path / "monitor_state.json")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 15, 12, 12, tzinfo=ZoneInfo("Asia/Kolkata"))

    monkeypatch.setattr("bot.telegram_commands.datetime", FixedDatetime)
    handler = TelegramCommandHandler(cfg, FakeAlerter())  # type: ignore[arg-type]

    handler._dispatch(ParsedCommand("ping", ()))
    assert sent and "Pong" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("health", ()))
    assert sent and "Health check" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("or", ()))
    assert sent and "Opening Range" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("position", ()))
    assert sent and "FLAT" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("today", ()))
    assert sent and "Today" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("levels", ()))
    assert sent and "Levels vs OR" in sent[-1]

    sent.clear()
    handler._dispatch(ParsedCommand("next", ()))
    assert sent and "Next bar" in sent[-1]


def test_restart_calls_stop_then_start(monkeypatch):
    from zoneinfo import ZoneInfo

    sent: list[str] = []
    calls: list[str] = []

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    class FakeProcess:
        def stop(self) -> str:
            calls.append("stop")
            return "Not running."

        def start(self) -> str:
            calls.append("start")
            return "Market session started (pid 99)."

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    monkeypatch.setattr("bot.telegram_commands.datetime", FixedDatetime)
    monkeypatch.setattr("bot.telegram_commands.time.sleep", lambda _: None)
    handler = TelegramCommandHandler(_cfg(), FakeAlerter(), process=FakeProcess())  # type: ignore[arg-type]
    handler._dispatch(ParsedCommand("restart", ()))
    assert calls == ["stop", "start"]
    assert sent and "Restart:" in sent[0]


def test_ping_does_not_call_probe(tmp_path: Path, monkeypatch):
    from zoneinfo import ZoneInfo

    sent: list[str] = []
    probe_called = {"n": 0}

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    def fake_probe(*_args, **_kwargs):
        probe_called["n"] += 1
        raise AssertionError("probe should not run for /ping")

    monkeypatch.setattr("bot.telegram_commands.probe_market_feed", fake_probe)
    monkeypatch.setattr("bot.telegram_commands._project_root", lambda: tmp_path)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    monkeypatch.setattr("bot.telegram_commands.datetime", FixedDatetime)
    handler = TelegramCommandHandler(_cfg(), FakeAlerter())  # type: ignore[arg-type]
    handler._dispatch(ParsedCommand("ping", ()))
    assert probe_called["n"] == 0
    assert sent and "Pong" in sent[0]


def test_unknown_command_gets_reply():
    sent: list[str] = []

    class FakeAlerter:
        def send(self, text: str, *, parse_mode: str | None = None) -> None:
            sent.append(text)

    handler = TelegramCommandHandler(_cfg(), FakeAlerter())  # type: ignore[arg-type]
    handler.updates = MagicMock()
    handler.updates.fetch_updates.return_value = [{
        "update_id": 1,
        "message": {"chat": {"id": 12345}, "text": "/reboot"},
    }]
    handler.updates.acknowledge = MagicMock()
    handler.poll_once()
    assert sent and "Unknown command" in sent[0]


def test_can_manual_start_session_before_monitor_open():
    from zoneinfo import ZoneInfo

    from bot.scheduler import can_manual_start_session

    cfg = _cfg().strategy
    early = datetime(2026, 6, 11, 9, 12, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert can_manual_start_session(early, cfg)
    too_early = datetime(2026, 6, 11, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert not can_manual_start_session(too_early, cfg)


def test_scheduler_sleep_wakes_on_force_tick(tmp_path: Path, monkeypatch):
    from bot.scheduler import LiveScheduler

    monkeypatch.setattr("bot.telegram_commands._project_root", lambda: tmp_path)
    flag = tmp_path / "data/live/force_tick.request"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("x", encoding="utf-8")

    tick = {"t": 0.0}

    def mono() -> float:
        tick["t"] += 0.5
        return tick["t"]

    slept: list[float] = []
    monkeypatch.setattr("bot.scheduler.time.monotonic", mono)
    monkeypatch.setattr("bot.scheduler.time.sleep", lambda s: slept.append(s))

    cfg = AppConfig(upstox_access_token="x", telegram_bot_token="x", telegram_chat_id="x", poll_interval_sec=20)
    sched = LiveScheduler(cfg, MagicMock(), MagicMock())  # type: ignore[arg-type]
    sched._sleep_until_next_poll()
    assert sum(slept) < 5


def test_force_tick_flag_helpers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bot.telegram_commands._project_root", lambda: tmp_path)
    flag = Path("data/live/force_tick.request")
    assert not TelegramCommandHandler.force_tick_requested(flag)
    path = tmp_path / flag
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert TelegramCommandHandler.force_tick_requested(flag)
    TelegramCommandHandler.clear_force_tick(flag)
    assert not path.exists()
