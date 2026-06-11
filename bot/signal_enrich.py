"""Attach option strike/bid data to signal events."""

from __future__ import annotations

from typing import Literal

from bot.config import AppConfig
from bot.logger import SignalEvent
from bot.option_lookup import OptionQuote, lookup_for_signal
from bot.state import Position

StrikeMode = Literal["ATM", "ITM1", "ATM_OR_ITM1"]


def enrich_event(event: SignalEvent, cfg: AppConfig, position: Position | None = None) -> None:
    mode: StrikeMode = cfg.strike_mode  # type: ignore[assignment]
    if event.event_type in ("BUY_CE", "BUY_PE"):
        q = lookup_for_signal(event.event_type, event.price, cfg.upstox_access_token, strike_mode=mode)
        if q:
            event.extra = {**(event.extra or {}), **q.to_extra()}
    elif position and position.option_strike:
        event.extra = {
            **(event.extra or {}),
            "option_strike": position.option_strike,
            "option_type": "CE" if position.side.value == "CE" else "PE",
            "option_expiry": position.option_expiry,
            "option_symbol": position.option_symbol,
            "option_bid": position.option_bid,
            "option_ask": position.option_ask,
        }


def snapshot_option(position: Position) -> dict:
    if not position.option_strike:
        return {}
    return {
        "option_strike": position.option_strike,
        "option_type": "CE" if position.side.value == "CE" else "PE",
        "option_expiry": position.option_expiry,
        "option_symbol": position.option_symbol,
        "option_bid": position.option_bid,
        "option_ask": position.option_ask,
    }


def save_option_on_position(position: Position, event: SignalEvent) -> None:
    extra = event.extra or {}
    if event.event_type not in ("BUY_CE", "BUY_PE"):
        return
    position.option_strike = extra.get("option_strike")
    position.option_expiry = extra.get("option_expiry")
    position.option_symbol = extra.get("option_symbol")
    position.option_bid = extra.get("option_bid")
    position.option_ask = extra.get("option_ask")
