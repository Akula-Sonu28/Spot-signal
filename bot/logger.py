"""Structured event logging for replay and live monitoring."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SignalEvent:
    timestamp: datetime
    event_type: str
    direction: str
    side: str
    price: float
    stop: float | None = None
    target: float | None = None
    reason: str = ""
    bar_index: int = -1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class ReplayLogger:
    def __init__(self) -> None:
        self.events: list[SignalEvent] = []

    def log(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        direction: str,
        side: str,
        price: float,
        stop: float | None = None,
        target: float | None = None,
        reason: str = "",
        bar_index: int = -1,
        **extra: Any,
    ) -> SignalEvent:
        event = SignalEvent(
            timestamp=timestamp,
            event_type=event_type,
            direction=direction,
            side=side,
            price=price,
            stop=stop,
            target=target,
            reason=reason,
            bar_index=bar_index,
            extra=extra,
        )
        self.events.append(event)
        return event

    def filter(self, event_type: str) -> list[SignalEvent]:
        return [e for e in self.events if e.event_type == event_type]

    def write_csv(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.events:
            if not path.exists():
                path.write_text("")
            return

        fieldnames = [
            "timestamp",
            "event_type",
            "direction",
            "side",
            "price",
            "stop",
            "target",
            "reason",
            "bar_index",
            "extra",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for e in self.events:
                row = e.to_dict()
                row["extra"] = json.dumps(row.get("extra", {}))
                writer.writerow(row)


LIVE_CSV_FIELDS = [
    "timestamp",
    "event_type",
    "direction",
    "side",
    "price",
    "stop",
    "target",
    "or_high",
    "or_low",
    "adx",
    "vwap",
    "atr",
    "reason",
    "bar_index",
    "option_strike",
    "option_type",
    "option_expiry",
    "option_symbol",
    "option_bid",
    "option_ask",
    "option_ltp",
    "option_spread",
    "option_oi",
    "option_note",
    "bars_in_trade",
    "spot_from_entry",
    "chop_streak",
    "high_theta_decay_risk",
    "premium_drawdown_pct",
    "max_premium_drawdown_pct",
    "max_spot_adverse_pts",
    "premium_bleed_worse_than_spot",
    "wick_would_sl",
    "is_expiry_day",
    "target_held_on_close",
    "target_reverted_mid_bar",
    "entry_slippage_pts",
    "spread_drag_pct",
    "iv_at_entry",
    "iv_delta",
    "iv_crush_bleed",
    "extra",
]


class LiveEventLogger:
    """Append-only CSV logger for live signals."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=LIVE_CSV_FIELDS).writeheader()

    def append(self, event: SignalEvent) -> None:
        extra = event.extra or {}
        option_keys = {
            "option_strike", "option_type", "option_expiry", "option_symbol",
            "option_ltp", "option_bid", "option_ask", "option_spread", "option_oi", "option_note",
        }
        diag_keys = {
            "bars_in_trade", "spot_from_entry", "chop_streak", "high_theta_decay_risk",
            "premium_drawdown_pct", "max_premium_drawdown_pct", "max_spot_adverse_pts",
            "premium_bleed_worse_than_spot", "wick_would_sl", "is_expiry_day",
            "target_held_on_close", "target_reverted_mid_bar", "entry_slippage_pts",
            "spread_drag_pct", "iv_at_entry", "iv_delta", "iv_crush_bleed",
        }
        row = {
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type,
            "direction": event.direction,
            "side": event.side,
            "price": event.price,
            "stop": event.stop,
            "target": event.target,
            "or_high": extra.get("or_high"),
            "or_low": extra.get("or_low"),
            "adx": extra.get("adx"),
            "vwap": extra.get("vwap"),
            "atr": extra.get("atr"),
            "reason": event.reason,
            "bar_index": event.bar_index,
            "extra": json.dumps({
                k: v for k, v in extra.items()
                if k not in {"or_high", "or_low", "adx", "vwap", "atr", *option_keys, *diag_keys}
            }),
        }
        for k in option_keys | diag_keys:
            if k in extra:
                row[k] = extra.get(k)
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LIVE_CSV_FIELDS).writerow(row)

    def log_system(self, event_type: str, message: str) -> None:
        self.append(
            SignalEvent(
                timestamp=datetime.now().astimezone(),
                event_type=event_type,
                direction="SYSTEM",
                side="NONE",
                price=0.0,
                reason=message,
            )
        )
