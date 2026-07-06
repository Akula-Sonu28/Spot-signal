"""Attach option strike/bid data and microstructure telemetry to signal events."""

from __future__ import annotations

from typing import Any, Literal

from bot.config import AppConfig
from bot.logger import SignalEvent
from bot.option_lookup import OptionQuote, lookup_by_contract, lookup_for_signal
from bot.option_truth import (
    SPREAD_DRAG_WARN_PCT,
    audit_target_wick_close,
    build_entry_telemetry,
    evaluate_velocity_chop,
    init_premium_lifecycle,
    iv_crush_bleed,
    iv_delta_from_entry,
)
from bot.premium_decay_diag import _spot_adverse_pts
from bot.state import Position

StrikeMode = Literal["ATM", "ITM1", "ATM_OR_ITM1"]

# Keys promoted to live CSV columns
TRUTH_LAYER_EXTRA_KEYS = frozenset({
    "target_held_on_close",
    "target_reverted_mid_bar",
    "entry_slippage_pts",
    "spread_drag_pct",
    "iv_at_entry",
    "iv_delta",
    "iv_crush_bleed",
    "high_spread_drag",
    "telemetry_flags",
})

PREMIUM_DIAG_EXTRA_KEYS = frozenset({
    "bars_in_trade",
    "spot_from_entry",
    "in_chop_band",
    "chop_streak",
    "wick_would_sl",
    "high_theta_decay_risk",
    "max_spot_adverse_pts",
    "max_premium_drawdown_pct",
    "premium_drawdown_pct",
    "premium_bleed_worse_than_spot",
    "spot_drawdown_pts",
    "is_expiry_day",
})


def _attach_telemetry_flags(extra: dict[str, Any], position: Position | None) -> None:
    """Informational monitor annotations — no session flow impact."""
    flags: list[str] = list(extra.get("telemetry_flags") or [])
    if extra.get("iv_crush_bleed"):
        flags.append("iv_crush_bleed")
    if extra.get("high_spread_drag") or (position and position.high_spread_drag):
        flags.append("high_spread_drag")
    if position and position.iv_crush_bleed:
        flags.append("iv_crush_bleed")
    if flags:
        extra["telemetry_flags"] = sorted(set(flags))


def enrich_event(
    event: SignalEvent,
    cfg: AppConfig,
    position: Position | None = None,
    *,
    bar: Any | None = None,
    bar_index: int | None = None,
    session_date: str | None = None,
) -> None:
    mode: StrikeMode = cfg.strike_mode  # type: ignore[assignment]
    if event.event_type in ("BUY_CE", "BUY_PE"):
        q = lookup_for_signal(event.event_type, event.price, cfg.upstox_access_token, strike_mode=mode, session_date=session_date)
        if q:
            event.extra = {**(event.extra or {}), **q.to_extra()}
            enrich_entry_slippage(event, q, bar=bar)
    elif event.event_type in ("TARGET_CE", "TARGET_PE") and bar is not None:
        enrich_target_wick_audit(event, bar)
    elif event.event_type == "POSITION_PREMIUM" and position:
        enrich_position_premium(event, position, cfg, bar_index=bar_index)
    elif position and position.option_strike:
        event.extra = {**(event.extra or {}), **snapshot_option(position)}
    _attach_telemetry_flags(event.extra or {}, position)


def enrich_entry_slippage(event: SignalEvent, quote: OptionQuote, *, bar: Any | None = None) -> None:
    alert_spot = float(bar.close) if bar is not None else event.price
    tel = build_entry_telemetry(
        signal_bar_close=event.price,
        alert_spot=alert_spot,
        ask=quote.entry_ask(),
        bid=quote.bid,
        delta=quote.delta,
        side=event.side,
        iv=quote.iv,
    )
    event.extra = {**(event.extra or {}), **tel}
    if tel.get("spread_drag_pct", 0) > SPREAD_DRAG_WARN_PCT:
        event.extra["high_spread_drag"] = True


def enrich_target_wick_audit(event: SignalEvent, bar: Any) -> None:
    audit = audit_target_wick_close(
        event_type=event.event_type,
        target=event.target,
        bar_high=float(bar.high),
        bar_low=float(bar.low),
        bar_close=float(bar.close),
    )
    if audit:
        event.extra = {**(event.extra or {}), **audit}


def enrich_position_premium(event: SignalEvent, position: Position, cfg: AppConfig, *, bar_index: int | None = None) -> None:
    """Live: fetch held contract quote; track premium, IV crush, spread drag."""
    if not position.option_strike or not position.option_expiry:
        return
    opt_type = "CE" if position.side.value == "CE" else "PE"
    q = lookup_by_contract(
        position.option_strike,
        opt_type,  # type: ignore[arg-type]
        position.option_expiry,
        cfg.upstox_access_token,
    )
    if not q.quote_ok and q.ltp is None:
        event.extra = {**(event.extra or {}), **q.to_extra()}
        return

    entry_prem = position.option_entry_premium or position.option_ask
    ltp = q.ltp or q.bid
    if ltp is not None:
        position.option_ltp = ltp

    extra = {**(event.extra or {}), **q.to_extra()}
    entry = position.entry_price or event.price
    spot_dd = _spot_adverse_pts(position.side.value, entry, event.price)
    position.max_spot_adverse_pts = max(position.max_spot_adverse_pts, spot_dd)
    extra["spot_drawdown_pts"] = round(spot_dd, 2)
    extra["max_spot_adverse_pts"] = round(position.max_spot_adverse_pts, 2)

    spot_from_entry = (
        event.price - entry if position.side.value == "CE" else entry - event.price
    )
    extra["spot_from_entry"] = round(spot_from_entry, 2)

    if position.iv_at_entry is not None:
        extra["iv_at_entry"] = round(position.iv_at_entry, 4)
    elif q.iv is not None:
        extra["iv_at_entry"] = round(q.iv, 4)

    iv_now = q.iv
    iv_entry = position.iv_at_entry if position.iv_at_entry is not None else q.iv
    iv_d = iv_delta_from_entry(iv_entry, iv_now)
    if iv_d is not None:
        extra["iv_delta"] = iv_d
        position.last_iv_delta = iv_d
    if iv_crush_bleed(
        iv_at_entry=iv_entry,
        iv_current=iv_now,
        spot_from_entry=spot_from_entry,
        atr=extra.get("atr"),
    ):
        position.iv_crush_bleed = True
        extra["iv_crush_bleed"] = True

    drag = None
    if q.ask and q.bid:
        from bot.option_truth import spread_drag_pct

        drag = spread_drag_pct(q.ask, q.bid)
    if drag is not None:
        extra["spread_drag_pct"] = drag
        if drag > SPREAD_DRAG_WARN_PCT:
            position.high_spread_drag = True
            extra["high_spread_drag"] = True

    if entry_prem and entry_prem > 0 and ltp is not None:
        prem_dd_pct = max(0.0, (entry_prem - ltp) / entry_prem * 100.0)
        position.max_premium_drawdown_pct = max(position.max_premium_drawdown_pct, prem_dd_pct)
        extra["premium_drawdown_pct"] = round(prem_dd_pct, 1)
        extra["max_premium_drawdown_pct"] = round(position.max_premium_drawdown_pct, 1)
        if spot_dd > 0 and prem_dd_pct > 15:
            implied = spot_dd * 0.5
            if (entry_prem - ltp) > implied * 1.5:
                position.premium_bleed_worse_than_spot = True
        extra["premium_bleed_worse_than_spot"] = position.premium_bleed_worse_than_spot

    # Evaluate velocity chop stop (Phase 2)
    if bar_index is not None and hasattr(position, 'premium_velocity_min'):
        chop_result = evaluate_velocity_chop(position, ltp, bar_index)
        if chop_result.triggered:
            extra["chop_stop_exit"] = True
    
    _attach_telemetry_flags(extra, position)
    event.extra = extra


def snapshot_option(position: Position) -> dict:
    if not position.option_strike:
        return {}
    out = {
        "option_strike": position.option_strike,
        "option_type": "CE" if position.side.value == "CE" else "PE",
        "option_expiry": position.option_expiry,
        "option_symbol": position.option_symbol,
        "option_bid": position.option_bid,
        "option_ask": position.option_ask,
    }
    if position.option_ltp is not None:
        out["option_ltp"] = position.option_ltp
    if position.option_entry_premium is not None:
        out["option_entry_premium"] = position.option_entry_premium
    if position.iv_at_entry is not None:
        out["iv_at_entry"] = position.iv_at_entry
    return out


def save_option_on_position(position: Position, event: SignalEvent) -> None:
    extra = event.extra or {}
    if event.event_type not in ("BUY_CE", "BUY_PE"):
        return
    position.option_strike = extra.get("option_strike")
    position.option_expiry = extra.get("option_expiry")
    position.option_symbol = extra.get("option_symbol")
    position.option_bid = extra.get("option_bid")
    position.option_ask = extra.get("option_ask")
    ask = extra.get("option_ask")
    position.option_entry_premium = float(ask) if ask else None
    iv = extra.get("option_iv") if extra.get("option_iv") is not None else extra.get("iv_at_entry")
    position.iv_at_entry = float(iv) if iv is not None else None
    position.chop_streak = 0
    position.max_spot_adverse_pts = 0.0
    position.max_premium_drawdown_pct = 0.0
    position.premium_bleed_worse_than_spot = False
    position.iv_crush_bleed = False
    position.high_spread_drag = bool(extra.get("high_spread_drag", False))
    position.last_iv_delta = None
    
    # Initialize premium lifecycle after ask captured (Phase 2)
    if ask and ask > 0:
        init_premium_lifecycle(position, float(ask))
