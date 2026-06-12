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
    monkeypatch.setattr(proc, "is_running", lambda: False)
    assert "Starting" in proc.start()
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

    handler = TelegramCommandHandler(cfg, FakeAlerter(), process=process)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "bot.telegram_commands._project_root",
        lambda: tmp_path,
    )
    handler.force_tick_flag = Path("force_tick.request")

    handler._dispatch(ParsedCommand("status", ()))
    assert sent and "nifty signal engine" in sent[0].lower()

    sent.clear()
    handler._dispatch(ParsedCommand("tick", ()))
    assert sent and "Tick requested" in sent[0]
    assert (tmp_path / "force_tick.request").exists()

    sent.clear()
    handler._dispatch(ParsedCommand("help", ()))
    assert "/stop" in sent[0]


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
