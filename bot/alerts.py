"""Telegram notifications for live signal monitoring."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from bot.config import AppConfig
from bot.logger import SignalEvent
from bot.option_lookup import OptionQuote, format_option_lines, format_premium_risk_lines
from bot.state import Position


class TelegramAlerter:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"

    def send(self, text: str, *, parse_mode: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(f"{self._base}/sendMessage", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
            if not body.get("ok"):
                raise RuntimeError(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc

    def bot_started(self) -> None:
        self.send(
            "🟢 NIFTY Signal Engine started\n"
            "Mode: ALERTS ONLY (AUTO_TRADE=False)\n"
            "Instrument: NIFTY 50 | TF: 5m | TZ: IST\n"
            "Monitoring: 09:15–15:30 | Signals: 09:30–15:15"
        )

    def bot_stopped(self, reason: str = "shutdown") -> None:
        self.send(f"🔴 NIFTY Signal Engine stopped\nReason: {reason}")

    def or_ready(self, or_high: float, or_low: float, width: float) -> None:
        self.send(
            "📊 Opening range ready\n"
            f"OR High: {or_high:.2f}\n"
            f"OR Low:  {or_low:.2f}\n"
            f"Width:   {width:.2f} pts\n"
            "Signal window open from 09:30"
        )

    def data_error(self, message: str, *, during_signal_hours: bool = False) -> None:
        header = "🚨 Data/API warning (SIGNAL HOURS)" if during_signal_hours else "⚠️ Data/API warning"
        self.send(f"{header}\n{message}\nNo signals generated for this tick.")

    def signals_paused(self, minutes_without_bars: int) -> None:
        self.send(
            "🚨 Signals paused — no candles processed\n"
            f"No 5m bars synced for {minutes_without_bars}+ minutes during signal window.\n"
            "Check Upstox feed and bot logs. You may miss trades until data recovers."
        )

    def signal_event(self, event: SignalEvent, position: Position | None = None) -> None:
        self.send(self.format_signal(event, self.cfg, position))

    @staticmethod
    def format_signal(
        event: SignalEvent,
        cfg: AppConfig | None = None,
        position: Position | None = None,
    ) -> str:
        extra = event.extra or {}
        et = event.event_type
        lines = [f"🔔 {et}"]
        if extra.get("catch_up"):
            lines.append("Catch-up alert — bot synced missed bars; act only if levels still valid.")
        strike_mode = cfg.strike_mode if cfg else "ATM_OR_ITM1"

        option_q: OptionQuote | None = None
        if extra.get("option_strike"):
            option_q = OptionQuote(
                strike=int(extra["option_strike"]),
                option_type=str(extra.get("option_type", "CE")),
                expiry=str(extra.get("option_expiry", "")),
                trading_symbol=str(extra.get("option_symbol", "")),
                instrument_key="",
                ltp=extra.get("option_ltp"),
                bid=extra.get("option_bid"),
                ask=extra.get("option_ask"),
                spread=extra.get("option_spread"),
                oi=extra.get("option_oi"),
                quote_ok=extra.get("option_bid") is not None,
                note=str(extra.get("option_note", "")),
            )
        elif position and position.option_strike:
            option_q = OptionQuote(
                strike=int(position.option_strike),
                option_type="CE" if position.side.value == "CE" else "PE",
                expiry=str(position.option_expiry or ""),
                trading_symbol=str(position.option_symbol or ""),
                instrument_key="",
                bid=position.option_bid,
                ask=position.option_ask,
                quote_ok=position.option_bid is not None,
                note="From entry snapshot",
            )

        rr = cfg.strategy.rr_ratio if cfg else 1.8

        if et in ("BUY_CE", "BUY_PE"):
            lines += [
                f"Action: Buy {'CALL (CE)' if et == 'BUY_CE' else 'PUT (PE)'} — manual execution",
                "",
                "——— OPTION ———",
                *format_option_lines(option_q, strike_mode),  # type: ignore[arg-type]
                "",
                "——— OPTION RISK (from ask) ———",
                *format_premium_risk_lines(option_q, rr_ratio=rr),
                "",
                "——— SPOT ———",
                f"Spot entry: {event.price:.2f}",
                f"Spot SL: {event.stop:.2f} | Spot target: {event.target:.2f}",
                f"OR: {extra.get('or_high', '—')} / {extra.get('or_low', '—')}",
                f"ADX: {extra.get('adx', '—')} | VWAP: {extra.get('vwap', '—')}",
                f"Reason: {event.reason}",
            ]
        elif et in ("TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE"):
            lines += [
                f"Exit @ spot {event.price:.2f}",
                f"Spot entry was: {extra.get('entry', '—')}",
                f"Spot SL: {event.stop} | Spot target: {event.target}",
            ]
            if option_q:
                entry_ask = extra.get("option_ask") or option_q.entry_ask()
                lines += [
                    "",
                    "——— OPTION (at entry) ———",
                    *format_option_lines(option_q, strike_mode),
                    *format_premium_risk_lines(option_q, entry_ask=entry_ask, rr_ratio=rr),
                ]
            lines.append(f"Reason: {event.reason}")
        elif et == "SQUARE_OFF":
            lines += [
                "EOD square-off — close open position",
                f"Spot: {event.price:.2f}",
                f"Spot entry was: {extra.get('entry', '—')}",
            ]
            if option_q:
                entry_ask = extra.get("option_ask") or option_q.entry_ask()
                lines += [
                    "",
                    "——— OPTION (at entry) ———",
                    *format_option_lines(option_q, strike_mode),
                    *format_premium_risk_lines(option_q, entry_ask=entry_ask, rr_ratio=rr),
                ]
        else:
            lines.append(json.dumps(event.to_dict(), default=str))

        lines.append(f"Time: {event.timestamp.strftime('%Y-%m-%d %H:%M IST')}")
        return "\n".join(lines)
