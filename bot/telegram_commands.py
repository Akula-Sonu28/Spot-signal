"""Telegram remote commands for starting/stopping the live monitor."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter, _urlopen
from bot.config import AppConfig
from bot.platform_paths import (
    default_market_session_script,
    popen_session_kwargs,
    process_exists,
    session_start_command,
    terminate_process,
)
from bot.scheduler import after_monitor_close, can_manual_start_session
from bot.telegram_status import (
    TodayContext,
    format_health,
    format_levels,
    format_next,
    format_or,
    format_ping,
    format_position,
    format_status,
    format_today,
    load_today_context,
    probe_market_feed,
)

FORCE_TICK_FLAG = Path("data/live/force_tick.request")
DEFAULT_OFFSET_FILE = Path("data/live/telegram_offset.json")
DEFAULT_LOCK_FILE = Path("data/live/market-session.lock")
DEFAULT_REMOTE_LOCK_FILE = Path("data/live/telegram-remote.lock")
DEFAULT_SESSION_LOG = Path("data/live/market-session.log")
DEFAULT_START_SCRIPT = default_market_session_script()
SESSION_START_WAIT_SEC = 15
RESTART_WAIT_SEC = 3

COMMAND_ALIASES = {"pos": "position"}
KNOWN_COMMANDS = frozenset({
    "start", "stop", "status", "help", "tick",
    "ping", "health", "or", "position", "today", "levels", "next", "restart",
})


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
    name = COMMAND_ALIASES.get(name, name)
    if name not in KNOWN_COMMANDS:
        return None
    return ParsedCommand(name=name, args=tuple(parts[1:]))


def is_authorized(chat_id: int | str, cfg: AppConfig) -> bool:
    return str(chat_id) == str(cfg.telegram_chat_id).strip()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _remote_listener_pid(remote_lock: Path = DEFAULT_REMOTE_LOCK_FILE) -> int | None:
    """Return PID of the always-on Telegram remote listener, if running."""
    path = _project_root() / remote_lock
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    return pid if process_exists(pid) else None


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
        for _ in range(SESSION_START_WAIT_SEC):
            if self.is_running():
                pid = self.running_pid()
                return f"Market session started (pid {pid}). Startup Telegram within ~30s."
            time.sleep(1)
        return (
            "Start command sent — session lock not seen yet.\n"
            "Send /status again in ~30s (check data/live/market-session.log if still stopped)."
        )

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
            with _urlopen(req, timeout=max(25, timeout_sec + 5)) as resp:
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
                if text.strip().startswith("/"):
                    self.alerter.send("Unknown command. Send /help for the list.")
                elif text.strip():
                    self.alerter.send("Commands start with /. Try /help")
                continue
            self._dispatch(cmd)
            handled += 1
        self.updates.acknowledge(updates)
        return handled

    def _today_context(self) -> TodayContext:
        state_path = _project_root() / self.cfg.state_file
        return load_today_context(
            self.cfg,
            state_path,
            now=datetime.now(self.zone),
            market_pid=self.process.running_pid(),
            remote_pid=_remote_listener_pid(),
        )

    def _signals_csv_path(self) -> Path:
        path = self.cfg.event_log_csv
        return path if path.is_absolute() else _project_root() / path

    def _dispatch(self, cmd: ParsedCommand) -> None:
        try:
            self._dispatch_inner(cmd)
        except Exception as exc:
            self.alerter.send(f"Command failed ({cmd.name}): {type(exc).__name__}")

    def _dispatch_inner(self, cmd: ParsedCommand) -> None:
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
            if not can_manual_start_session(now, cfg):
                self.alerter.send(
                    "⏳ Too early — manual start from 09:10 IST\n"
                    "Monitoring runs 09:15–15:30; signals from 09:30.\n"
                    "Scheduled task also auto-starts at 09:10."
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
            return
        if cmd.name == "ping":
            self.alerter.send(format_ping(self._today_context()))
            return
        if cmd.name == "health":
            ctx = self._today_context()
            probe = probe_market_feed(self.cfg, ctx.now)
            self.alerter.send(format_health(ctx, self.cfg, probe, self._signals_csv_path()))
            return
        if cmd.name == "or":
            self.alerter.send(format_or(self._today_context(), self.cfg))
            return
        if cmd.name == "position":
            ctx = self._today_context()
            probe = probe_market_feed(self.cfg, ctx.now)
            self.alerter.send(format_position(ctx, self.cfg, probe))
            return
        if cmd.name == "today":
            ctx = self._today_context()
            self.alerter.send(format_today(ctx, self.cfg, self._signals_csv_path()))
            return
        if cmd.name == "levels":
            ctx = self._today_context()
            probe = probe_market_feed(self.cfg, ctx.now)
            self.alerter.send(format_levels(ctx, self.cfg, probe))
            return
        if cmd.name == "next":
            self.alerter.send(format_next(self._today_context(), self.cfg))
            return
        if cmd.name == "restart":
            self.alerter.send(self._restart_session())
            return

    def _help_text(self) -> str:
        return (
            "📟 NIFTY Signal Engine — remote commands\n"
            "\n"
            "Session\n"
            "/start — start market session (from 09:10 IST)\n"
            "/stop — stop running session\n"
            "/restart — stop, wait, then start again\n"
            "/status — full bot state snapshot\n"
            "\n"
            "Market\n"
            "/or — opening range, mode, breakout lines\n"
            "/position (/pos) — open trade only\n"
            "/today — today's signals from log\n"
            "/levels — spot vs OR distances\n"
            "/next — next 5m bar close time\n"
            "\n"
            "Debug\n"
            "/ping — remote alive check\n"
            "/health — diagnostics (remote, bot, feed, errors)\n"
            "/tick — force immediate signal poll\n"
            "/help — this message"
        )

    def _status_text(self) -> str:
        return format_status(self._today_context(), self.cfg)

    def _restart_session(self) -> str:
        now = datetime.now(self.zone)
        cfg = self.cfg.strategy
        if after_monitor_close(now, cfg):
            return (
                "⏸ Market session is closed for today\n"
                "Monitoring: 09:15–15:30 IST (Mon–Fri).\n"
                "Bot auto-starts tomorrow at 09:10."
            )
        if not can_manual_start_session(now, cfg):
            return (
                "⏳ Too early — manual restart from 09:10 IST\n"
                "Monitoring runs 09:15–15:30; signals from 09:30."
            )
        stop_msg = self.process.stop()
        time.sleep(RESTART_WAIT_SEC)
        start_msg = self.process.start()
        return f"Restart:\n1) {stop_msg}\n2) {start_msg}"

    def _request_tick(self) -> str:
        if not self.process.is_running():
            return "Session not running. Send /start first."
        path = _project_root() / self.force_tick_flag
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(self.zone).isoformat(), encoding="utf-8")
        return "Tick requested — polling within ~2s (wake from sleep)."

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

    # Only announce startup when this is a fresh start, not a crash-restart.
    # We detect a fresh start by checking if the offset file is new/absent or
    # if no updates have been processed yet this session.
    try:
        handler.alerter.send(
            "📟 Remote control online\n"
            "Send /help for all commands."
        )
    except Exception:
        pass  # Don't die if Telegram is unreachable at startup

    while True:
        try:
            handler.poll_once(blocking_timeout_sec=25)
        except Exception as exc:
            try:
                handler.alerter.send(f"⚠️ Remote control error\n{exc}")
            except Exception:
                pass
            time.sleep(poll_interval_sec)
        time.sleep(poll_interval_sec)
