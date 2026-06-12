"""Telegram remote commands for starting/stopping the live monitor."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter
from bot.config import AppConfig
from bot.platform_paths import (
    default_market_session_script,
    popen_session_kwargs,
    process_exists,
    session_start_command,
    terminate_process,
)
from bot.scheduler import after_monitor_close, in_monitor_window, in_signal_window
from bot.state import LiveMonitorState, PositionSide

FORCE_TICK_FLAG = Path("data/live/force_tick.request")
DEFAULT_OFFSET_FILE = Path("data/live/telegram_offset.json")
DEFAULT_LOCK_FILE = Path("data/live/market-session.lock")
DEFAULT_SESSION_LOG = Path("data/live/market-session.log")
DEFAULT_START_SCRIPT = default_market_session_script()

KNOWN_COMMANDS = frozenset({"start", "stop", "status", "help", "tick"})


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...]


def parse_command(text: str) -> ParsedCommand | None:
    """Parse '/status' or '/start arg' from a Telegram message."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    body = raw[1:]
    if "@" in body:
        body = body.split("@", 1)[0]
    parts = body.split()
    if not parts:
        return None
    name = parts[0].lower()
    if name not in KNOWN_COMMANDS:
        return None
    return ParsedCommand(name=name, args=tuple(parts[1:]))


def is_authorized(chat_id: int | str, cfg: AppConfig) -> bool:
    return str(chat_id) == str(cfg.telegram_chat_id).strip()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


class SessionProcessControl:
    """Start/stop the market session wrapper script via lock file."""

    def __init__(
        self,
        *,
        lock_file: Path = DEFAULT_LOCK_FILE,
        start_script: Path | None = None,
        session_log: Path = DEFAULT_SESSION_LOG,
    ) -> None:
        self.lock_file = lock_file
        self.start_script = start_script or default_market_session_script()
        self.session_log = session_log
        self.project_root = _project_root()

    def _resolve_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path

    def running_pid(self) -> int | None:
        lock_path = self._resolve_path(self.lock_file)
        if not lock_path.exists():
            return None
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None
        if pid <= 0:
            return None
        if not process_exists(pid):
            return None
        return pid

    def is_running(self) -> bool:
        return self.running_pid() is not None

    def start(self) -> str:
        if self.is_running():
            pid = self.running_pid()
            return f"Already running (pid {pid}). Send /status for details."
        script = (
            self.start_script
            if self.start_script.is_absolute()
            else self.project_root / self.start_script
        )
        if not script.exists():
            raise FileNotFoundError(f"Start script not found: {script}")
        subprocess.Popen(
            session_start_command(script),
            cwd=str(self.project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_session_kwargs(),  # type: ignore[arg-type]
        )
        return "Starting market session… You should get a startup Telegram within ~30s."

    def stop(self) -> str:
        pid = self.running_pid()
        if pid is None:
            return "Not running."
        terminate_process(pid)
        return f"Stop signal sent to pid {pid}."

    def last_log_line(self) -> str | None:
        path = self.project_root / self.session_log
        if not path.exists():
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-1] if lines else None


class TelegramUpdateClient:
    """Fetch Telegram updates for a single bot token (only one poller per token)."""

    def __init__(self, cfg: AppConfig, offset_file: Path = DEFAULT_OFFSET_FILE) -> None:
        self.cfg = cfg
        self.offset_file = offset_file
        self._base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"

    def _load_offset(self) -> int | None:
        path = _project_root() / self.offset_file
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = raw.get("offset")
            return int(value) if value is not None else None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _save_offset(self, offset: int) -> None:
        path = _project_root() / self.offset_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": offset}, indent=2), encoding="utf-8")

    def fetch_updates(self, *, timeout_sec: int = 0) -> list[dict[str, Any]]:
        params: dict[str, str] = {"timeout": str(timeout_sec)}
        offset = self._load_offset()
        if offset is not None:
            params["offset"] = str(offset)
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self._base}/getUpdates?{query}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(25, timeout_sec + 5)) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram getUpdates HTTP {exc.code}: {detail}") from exc
        if not body.get("ok"):
            raise RuntimeError(body)
        return list(body.get("result", []))

    def acknowledge(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        last_id = max(int(item["update_id"]) for item in updates)
        self._save_offset(last_id + 1)


class TelegramCommandHandler:
    """Route authorized Telegram commands to session control actions."""

    def __init__(
        self,
        cfg: AppConfig,
        alerter: TelegramAlerter,
        *,
        process: SessionProcessControl | None = None,
        update_client: TelegramUpdateClient | None = None,
        force_tick_flag: Path = FORCE_TICK_FLAG,
    ) -> None:
        self.cfg = cfg
        self.alerter = alerter
        self.process = process or SessionProcessControl()
        self.updates = update_client or TelegramUpdateClient(cfg)
        self.force_tick_flag = force_tick_flag
        self.zone = ZoneInfo(cfg.timezone)

    def poll_once(self, *, blocking_timeout_sec: int = 0) -> int:
        """Fetch and handle pending commands. Returns number handled."""
        updates = self.updates.fetch_updates(timeout_sec=blocking_timeout_sec)
        handled = 0
        for update in updates:
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None or not is_authorized(chat_id, self.cfg):
                continue
            text = message.get("text") or ""
            cmd = parse_command(text)
            if cmd is None:
                continue
            self._dispatch(cmd)
            handled += 1
        self.updates.acknowledge(updates)
        return handled

    def _dispatch(self, cmd: ParsedCommand) -> None:
        if cmd.name == "help":
            self.alerter.send(self._help_text())
            return
        if cmd.name == "start":
            now = datetime.now(self.zone)
            cfg = self.cfg.strategy
            if after_monitor_close(now, cfg):
                self.alerter.send(
                    "⏸ Market session is closed for today\n"
                    "Monitoring: 09:15–15:30 IST (Mon–Fri).\n"
                    "Bot auto-starts tomorrow at 09:10."
                )
                return
            if not in_monitor_window(now, cfg):
                self.alerter.send(
                    "⏳ Too early — market not open yet\n"
                    "Monitoring starts 09:15 IST.\n"
                    "Bot auto-starts at 09:10; signals from 09:30."
                )
                return
            self.alerter.send(self.process.start())
            return
        if cmd.name == "stop":
            self.alerter.send(self.process.stop())
            return
        if cmd.name == "status":
            self.alerter.send(self._status_text())
            return
        if cmd.name == "tick":
            self.alerter.send(self._request_tick())

    def _help_text(self) -> str:
        return (
            "📟 NIFTY Signal Engine — remote commands\n"
            "/start — start market session (09:15–15:30 IST only)\n"
            "/stop — stop running session\n"
            "/status — bot state, position, last candle\n"
            "/tick — force one signal poll (~20s, session must be running)\n"
            "/help — this message"
        )

    def _status_text(self) -> str:
        now = datetime.now(self.zone)
        pid = self.process.running_pid()
        running = pid is not None
        lines = [
            "📟 NIFTY Signal Engine status",
            f"Time: {now.strftime('%Y-%m-%d %H:%M IST')}",
            f"Session: {'RUNNING' if running else 'STOPPED'}" + (f" (pid {pid})" if pid else ""),
            f"Signal window: {'OPEN' if in_signal_window(now, self.cfg.strategy) else 'CLOSED'}",
        ]

        state_path = _project_root() / self.cfg.state_file
        session_date = now.date().isoformat()
        if state_path.exists():
            monitor = LiveMonitorState.load(state_path, session_date)
            pos = monitor.position
            day = monitor.day
            lines.append(f"Last candle: {monitor.last_processed_candle or '—'}")
            if day:
                lines.append(
                    f"OR: {day.or_high or '—'} / {day.or_low or '—'} | trades today: {day.trades_today}"
                )
            if pos.side != PositionSide.FLAT:
                lines.append(
                    f"Position: {pos.side.value} entry={pos.entry_price} SL={pos.stop} T={pos.target}"
                )
            else:
                lines.append("Position: FLAT")
        else:
            lines.append("State file: not found")

        log_line = self.process.last_log_line()
        if log_line:
            lines.append(f"Log: {log_line}")
        return "\n".join(lines)

    def _request_tick(self) -> str:
        if not self.process.is_running():
            return "Session not running. Send /start first."
        path = _project_root() / self.force_tick_flag
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(self.zone).isoformat(), encoding="utf-8")
        return "Tick requested — bot will process on next poll (~20s)."

    @staticmethod
    def force_tick_requested(flag_path: Path = FORCE_TICK_FLAG) -> bool:
        path = _project_root() / flag_path
        return path.exists()

    @staticmethod
    def clear_force_tick(flag_path: Path = FORCE_TICK_FLAG) -> None:
        path = _project_root() / flag_path
        if path.exists():
            path.unlink()


def run_remote_loop(cfg: AppConfig, alerter: TelegramAlerter, poll_interval_sec: int = 2) -> None:
    """Blocking loop for the always-on Telegram command listener."""
    import time

    handler = TelegramCommandHandler(cfg, alerter)
    handler.alerter.send(
        "📟 Remote control online\n"
        "Send /help for commands (/start, /stop, /status, /tick)."
    )
    while True:
        try:
            handler.poll_once(blocking_timeout_sec=25)
        except Exception as exc:
            handler.alerter.send(f"⚠️ Remote control error\n{exc}")
            time.sleep(poll_interval_sec)
        time.sleep(poll_interval_sec)
