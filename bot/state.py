"""Per-session state for replay and live monitoring."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo


class PositionSide(str, Enum):
    FLAT = "FLAT"
    CE = "CE"
    PE = "PE"


@dataclass
class Position:
    side: PositionSide = PositionSide.FLAT
    entry_price: float | None = None
    stop: float | None = None
    target: float | None = None
    entry_time: datetime | None = None
    entry_bar_index: int | None = None
    option_strike: int | None = None
    option_expiry: str | None = None
    option_symbol: str | None = None
    option_bid: float | None = None
    option_ask: float | None = None


@dataclass
class DayState:
    """Resets at the start of each trading session."""

    session_date: str
    or_high: float | None = None
    or_low: float | None = None
    or_defined: bool = False
    or_bars_seen: int = 0
    trades_today: int = 0
    fired_long_today: bool = False
    fired_short_today: bool = False
    last_entry_bar_index: int | None = None
    # v3.9 combined router + J+ trap state
    day_mode: str | None = None
    losses_today: int = 0
    touched_above_or: bool = False
    touched_below_or: bool = False
    trap_high: float | None = None
    trap_low: float | None = None
    first_trap_side: str | None = None
    active_strategy: str | None = None


@dataclass
class ReplayState:
    position: Position = field(default_factory=Position)
    day: DayState | None = None
    bar_index: int = -1


@dataclass
class LiveMonitorState:
    """Persisted live monitor state (survives restarts)."""

    session_date: str
    last_processed_candle: str | None = None
    or_alert_sent: bool = False
    dispatched_alert_keys: list[str] = field(default_factory=list)
    dispatched_early_watch_keys: list[str] = field(default_factory=list)
    last_data_warning_at: str | None = None
    entry_window_seen_without_bars: str | None = None
    signals_paused_alert_sent: bool = False
    position: Position = field(default_factory=Position)
    day: DayState | None = None
    bar_index: int = -1

    def to_replay_state(self) -> ReplayState:
        return ReplayState(position=self.position, day=self.day, bar_index=self.bar_index)

    def sync_from_replay(self, replay: ReplayState) -> None:
        self.position = replay.position
        self.day = replay.day
        self.bar_index = replay.bar_index

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, session_date: str) -> LiveMonitorState:
        if not path.exists():
            return cls(session_date=session_date, day=make_day_state(session_date))
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("session_date") != session_date:
            return cls(session_date=session_date, day=make_day_state(session_date))
        return cls._from_dict(raw, session_date)

    def _to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "last_processed_candle": self.last_processed_candle,
            "or_alert_sent": self.or_alert_sent,
            "dispatched_alert_keys": list(self.dispatched_alert_keys),
            "dispatched_early_watch_keys": list(self.dispatched_early_watch_keys),
            "last_data_warning_at": self.last_data_warning_at,
            "entry_window_seen_without_bars": self.entry_window_seen_without_bars,
            "signals_paused_alert_sent": self.signals_paused_alert_sent,
            "bar_index": self.bar_index,
            "position": _position_to_dict(self.position),
            "day": _day_to_dict(self.day) if self.day else None,
        }

    @classmethod
    def _from_dict(cls, raw: dict[str, Any], session_date: str) -> LiveMonitorState:
        day_raw = raw.get("day")
        day = _day_from_dict(day_raw) if day_raw else make_day_state(session_date)
        return cls(
            session_date=session_date,
            last_processed_candle=raw.get("last_processed_candle"),
            or_alert_sent=bool(raw.get("or_alert_sent", False)),
            dispatched_alert_keys=list(raw.get("dispatched_alert_keys", [])),
            dispatched_early_watch_keys=list(raw.get("dispatched_early_watch_keys", [])),
            last_data_warning_at=raw.get("last_data_warning_at"),
            entry_window_seen_without_bars=raw.get("entry_window_seen_without_bars"),
            signals_paused_alert_sent=bool(raw.get("signals_paused_alert_sent", False)),
            bar_index=int(raw.get("bar_index", -1)),
            position=_position_from_dict(raw.get("position", {})),
            day=day,
        )


def make_day_state(session_date: str) -> DayState:
    return DayState(session_date=session_date)


def reset_position(position: Position) -> None:
    position.side = PositionSide.FLAT
    position.entry_price = None
    position.stop = None
    position.target = None
    position.entry_time = None
    position.entry_bar_index = None
    position.option_strike = None
    position.option_expiry = None
    position.option_symbol = None
    position.option_bid = None
    position.option_ask = None


def position_snapshot(position: Position) -> dict[str, Any]:
    return {
        "side": position.side.value,
        "entry_price": position.entry_price,
        "stop": position.stop,
        "target": position.target,
        "entry_time": position.entry_time.isoformat() if position.entry_time else None,
        "entry_bar_index": position.entry_bar_index,
    }


def _dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _str_to_dt(value: str | None, tz: str = "Asia/Kolkata") -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def _position_to_dict(pos: Position) -> dict[str, Any]:
    return {
        "side": pos.side.value,
        "entry_price": pos.entry_price,
        "stop": pos.stop,
        "target": pos.target,
        "entry_time": _dt_to_str(pos.entry_time),
        "entry_bar_index": pos.entry_bar_index,
        "option_strike": pos.option_strike,
        "option_expiry": pos.option_expiry,
        "option_symbol": pos.option_symbol,
        "option_bid": pos.option_bid,
        "option_ask": pos.option_ask,
    }


def _position_from_dict(raw: dict[str, Any]) -> Position:
    return Position(
        side=PositionSide(raw.get("side", "FLAT")),
        entry_price=raw.get("entry_price"),
        stop=raw.get("stop"),
        target=raw.get("target"),
        entry_time=_str_to_dt(raw.get("entry_time")),
        entry_bar_index=raw.get("entry_bar_index"),
        option_strike=raw.get("option_strike"),
        option_expiry=raw.get("option_expiry"),
        option_symbol=raw.get("option_symbol"),
        option_bid=raw.get("option_bid"),
        option_ask=raw.get("option_ask"),
    )


def _day_to_dict(day: DayState) -> dict[str, Any]:
    return asdict(day)


def _day_from_dict(raw: dict[str, Any]) -> DayState:
    return DayState(**raw)
