"""Live signal notifications (Telegram, ntfy, or both)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any


def _make_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that works even behind a corporate HTTPS proxy."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    except Exception:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CONTEXT: ssl.SSLContext = _make_ssl_context()


def _urlopen(req: urllib.request.Request, timeout: int = 20):  # type: ignore[return]
    """urlopen with unverified SSL fallback for corporate proxy environments."""
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
    except ssl.SSLCertVerificationError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


from bot.config import LOCKED_STRATEGY_VERSION, AppConfig, has_ntfy_alerts, has_telegram_alerts
from bot.option_truth import SPREAD_DRAG_WARN_PCT, PREMIUM_STOP_MULT, PREMIUM_TARGET_MULT
from bot.early_watch import bar_close_time
from bot.logger import SignalEvent
from bot.option_lookup import OptionQuote, PREMIUM_SL_LOSS_FRAC, format_entry_pnl_lines
from bot.state import Position
from bot.telegram_format import (
    DIVIDER,
    bold,
    code,
    html_escape,
    html_to_plain,
    italic,
    price,
    pts_signed,
    section,
)


def _risk_reward_label(entry: float, stop: float | None, target: float | None) -> str:
    """Compute actual R:R from spot levels."""
    if stop is None or target is None:
        return "—"
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return "—"
    rr = reward / risk
    return bold(f"{rr:.1f}R")


def _spot_vs_or(price: float, or_high: float | None, or_low: float | None,
                side: str) -> str:
    """How far price broke out of OR."""
    if or_high is None or or_low is None:
        return ""
    if side == "CE":
        dist = price - or_high
        return f"+{dist:.0f} pts above OR High"
    else:
        dist = or_low - price
        return f"{dist:.0f} pts below OR Low"


def _premium_backup_line(entry_ask: float | None) -> str | None:
    """Rough premium stop — not tied to spot SL (see TRADING_GUIDE)."""
    if entry_ask is None or entry_ask <= 0:
        return None
    backup = round(entry_ask * (1.0 - PREMIUM_SL_LOSS_FRAC), 2)
    return (
        f"{bold('Backup')}: consider exit if premium −50% "
        f"(≈ {bold(f'₹{backup:.2f}')})"
    )


def _spot_exit_plan(stop: float | None, target: float | None) -> str:
    return (
        f"Exit when SPOT hits SL {price(stop)} or Target {price(target)}"
    )


def _bar_close_label(bar_open: datetime, *, bar_time_is_open: bool = True) -> str:
    close_ts = bar_close_time(bar_open, bar_time_is_open=bar_time_is_open)
    return close_ts.strftime("%H:%M IST")


def _spread_friction_lines(extra: dict) -> list[str]:
    """Entry alert: high bid-ask drag and estimated slippage (option truth layer)."""
    drag = extra.get("spread_drag_pct")
    if drag is None:
        return []
    try:
        drag_f = float(drag)
    except (TypeError, ValueError):
        return []
    if drag_f <= SPREAD_DRAG_WARN_PCT:
        return []
    lines = [f"⚠️ {bold(f'[HIGH SPREAD FRICTION: {drag_f:.1f}% Drag]')}"]
    slip = extra.get("entry_slippage_pts")
    if slip is not None:
        lines.append(f"📉 Est. Entry Slippage: ±{abs(float(slip)):.1f} Premium Pts")
    return lines


def _iv_crush_alarm_line(
    *,
    iv_crush_bleed: bool,
    iv_at_entry: float | None = None,
    iv_delta: float | None = None,
    html: bool = True,
) -> str | None:
    if not iv_crush_bleed:
        return None
    drop_pct: float | None = None
    if iv_at_entry and iv_at_entry > 0 and iv_delta is not None and iv_delta < 0:
        drop_pct = abs(iv_delta) / iv_at_entry * 100.0
    pct_text = f"{drop_pct:.1f}%" if drop_pct is not None else "—"
    body = (
        f"Premium is bleeding! IV is down {pct_text} while spot is consolidating sideways."
    )
    if html:
        return f"🚨 {bold('[IV CRUSH ALARM]')}: {body}"
    return f"🚨 [IV CRUSH ALARM]: {body}"


def position_status_iv_alarm_lines(position: Position) -> list[str]:
    """POSITION_STATUS (/position): IV crush callout below spot P&L."""
    line = _iv_crush_alarm_line(
        iv_crush_bleed=position.iv_crush_bleed,
        iv_at_entry=position.iv_at_entry,
        iv_delta=position.last_iv_delta,
        html=False,
    )
    return [line] if line else []


def _format_premium_brackets(position: Position) -> list[str]:
    """Premium target/stop/R:R block for position status (plain text for /position)."""
    if (not hasattr(position, 'premium_target_price') or
        not hasattr(position, 'premium_stop_price') or
        position.premium_target_price is None or
        position.premium_stop_price is None):
        return []

    target_price = position.premium_target_price
    stop_price = position.premium_stop_price
    ltp = position.option_ltp or 0.0

    # Distance = ltp - premium_target_price (signed)
    distance = ltp - target_price
    distance_str = f"+{distance:.1f}" if distance >= 0 else f"{distance:.1f}"

    # R:R calculation
    entry_ask = position.option_ask or (target_price / PREMIUM_TARGET_MULT) if target_price > 0 else 0
    if entry_ask > 0:
        reward = target_price - entry_ask
        risk = entry_ask - stop_price
        if risk > 0:
            rr_ratio = reward / risk
            rr_label = f"1:{rr_ratio:.2f}"
        else:
            rr_label = "1:∞"
    else:
        rr_label = "1:2.67"

    target_pct = int(round((PREMIUM_TARGET_MULT - 1.0) * 100))
    stop_pct = int(round((1.0 - PREMIUM_STOP_MULT) * 100))

    return [
        f"📈 Premium Target (+{target_pct}%): ₹{target_price:.2f} "
        f"(Current LTP: ₹{ltp:.2f} | Distance: {distance_str} pts)",
        f"📉 Premium Stop (-{stop_pct}%):   ₹{stop_price:.2f} "
        f"(Risk-Reward Ratio: {rr_label} Premium Space)",
    ]


def _target_wick_postmortem_line(extra: dict, event_type: str) -> str | None:
    if event_type not in ("TARGET_CE", "TARGET_PE"):
        return None
    if not extra.get("target_reverted_mid_bar"):
        return None
    return (
        "ℹ️ Note: Target was a mid-bar wick hit only. Spot close reverted inside the "
        "boundary. Live manual option fills may have experienced low probability execution."
    )


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
            with _urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
            if not body.get("ok"):
                raise RuntimeError(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc

    def _send_html(self, text: str) -> None:
        self.send(text, parse_mode="HTML")

    # ── System messages ───────────────────────────────────────────────────────

    def bot_started(self) -> None:
        self._send_html(
            f"🟢 {bold('NIFTY Signal Engine')}  •  {bold('LIVE')}  "
            f"({code(f'v{LOCKED_STRATEGY_VERSION}')})\n"
            f"{DIVIDER}\n"
            f"Mode:       {bold('ALERTS ONLY')}  (no auto-trade)\n"
            "Instrument: NIFTY 50  |  5m bars  |  IST\n"
            "Session:    09:15 – 15:30\n"
            "Signals:    09:30 – 15:15  (post-OR)\n"
            "Playbook:   set after OR  (v3.8 / J+ / skip)\n"
            "Alerts:     WATCH (forming bar) + BUY on bar close\n"
            f"{DIVIDER}\n"
            f"{italic('Waiting for Opening Range…')}"
        )

    def bot_stopped(self, reason: str = "shutdown") -> None:
        self._send_html(
            f"🔴 {bold('NIFTY Signal Engine')}  •  {bold('STOPPED')}\n"
            f"{DIVIDER}\n"
            f"Reason: {html_escape(reason)}"
        )

    def or_ready(
        self,
        or_high: float,
        or_low: float,
        width: float,
        *,
        day_mode: str = "V38",
        enable_j_plus: bool = True,
    ) -> None:
        if day_mode == "SKIP":
            mode_line = f"{bold('Mode: SKIP')} — OR too narrow, no trades today"
            action = italic("No entries for this session.")
        elif day_mode == "J_PLUS" and not enable_j_plus:
            mode_line = f"{bold('Mode: Wide OR')} — J+ disabled"
            action = (
                "OR width &gt; 100 pts but ENABLE_J_PLUS=false.\n"
                f"{italic('No entries for this session.')}"
            )
        elif day_mode == "J_PLUS":
            mode_line = f"{bold('Mode: J+ trap-fade')} (wide OR)"
            action = (
                "Fake-break trap fade only. Max 1 trade after ~10:00 IST.\n"
                f"{italic('v3.8 breakout module is OFF today.')}"
            )
        else:
            mode_line = f"{bold('Mode: v3.8 OR breakout')}"
            action = (
                f"📈 Breakout:  above {price(or_high)}  →  {bold('BUY CE')}\n"
                f"📉 Breakdown: below {price(or_low)}  →  {bold('BUY PE')}\n"
                "👀 WATCH_CE/PE on forming bar (heads-up only)\n"
                f"🔔 {bold('BUY_CE/PE')} on 5m bar close (~5 min after WATCH)\n"
                f"{italic('J+ trap module is OFF today.')}"
            )

        self._send_html(
            f"{section('📊 Opening Range Set')}\n"
            f"{DIVIDER}\n"
            f"OR High:  {price(or_high)}\n"
            f"OR Low:   {price(or_low)}\n"
            f"Width:    {bold(f'{width:.0f} pts')}\n"
            f"{mode_line}\n"
            f"{DIVIDER}\n"
            f"{action}\n"
            f"{italic('Signal window: 09:30 – 15:15 IST')}"
        )

    def data_error(self, message: str, *, during_signal_hours: bool = False) -> None:
        if during_signal_hours:
            header = section("🚨 Feed issue during SIGNAL HOURS")
        else:
            header = section("⚠️ Data / API warning")
        self._send_html(
            f"{header}\n"
            f"{DIVIDER}\n"
            f"{html_escape(message)}\n"
            f"{italic('No signals generated this tick.')}"
        )

    def signals_paused(self, minutes_without_bars: int) -> None:
        self._send_html(
            f"{section('🚨 Signals Paused')}\n"
            f"{DIVIDER}\n"
            f"No 5m bars for {bold(f'{minutes_without_bars}+ min')} during signal window.\n"
            f"⚠️ {bold('You may miss trades')} until the feed recovers.\n"
            f"Action: check Upstox status / bot log."
        )

    def or_break_pending(
        self,
        watch_type: str,
        spot: float,
        or_high: float,
        or_low: float,
        bar_open: datetime,
        *,
        now: datetime,
        vwap: float | None = None,
        adx: float | None = None,
        bar_time_is_open: bool = True,
    ) -> None:
        """Heads-up while the current 5m bar is breaking OR (not a confirmed entry)."""
        is_ce = watch_type == "WATCH_CE"
        side = "CE" if is_ce else "PE"
        action = "BUY CALL (CE)" if is_ce else "BUY PUT (PE)"
        close_ts = bar_close_time(bar_open, bar_time_is_open=bar_time_is_open)
        breakout = _spot_vs_or(spot, or_high, or_low, side)
        adx_str = f"{adx:.1f}" if adx is not None else "—"
        vwap_note = ""
        if vwap is not None:
            vwap_note = bold("above VWAP ✓") if spot > vwap else bold("below VWAP ✓")

        self._send_html(
            f"👀 {bold(watch_type)}  •  OR "
            f"{'breakout' if is_ce else 'breakdown'} forming\n"
            f"{DIVIDER}\n"
            f"⏰ Now: {bold(now.strftime('%H:%M IST'))}\n"
            f"⏳ Confirms at bar close: {bold(close_ts.strftime('%H:%M IST'))}\n"
            f"📌 If confirmed → {bold(action)}\n"
            f"{DIVIDER}\n"
            f"📍 Spot (live): {price(spot)}  ({html_escape(breakout)})\n"
            f"📊 OR High: {price(or_high)}  |  Low: {price(or_low)}\n"
            f"📡 {vwap_note or 'VWAP: —'}  |  ADX: {bold(adx_str)}\n"
            f"{DIVIDER}\n"
            f"⚠️ {bold('NOT an entry')} — wait for official BUY alert after bar close.\n"
            f"{italic('Price can recover before the candle closes.')}"
        )

    def signal_event(self, event: SignalEvent, position: Position | None = None) -> None:
        self._send_html(self.format_signal(event, self.cfg, position))

    # ── Signal formatting ─────────────────────────────────────────────────────

    @staticmethod
    def format_signal(
        event: SignalEvent,
        cfg: AppConfig | None = None,
        position: Position | None = None,
    ) -> str:
        extra = event.extra or {}
        et = event.event_type
        rr_ratio = cfg.strategy.rr_ratio if cfg else 2.0
        strike_mode = cfg.strike_mode if cfg else "ATM_OR_ITM1"

        # ── build OptionQuote ─────────────────────────────────────────────
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
                delta=extra.get("option_delta"),
                theta=extra.get("option_theta"),
                iv=extra.get("option_iv"),
                gamma=extra.get("option_gamma"),
                vega=extra.get("option_vega"),
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
            )

        # ── route to formatter ────────────────────────────────────────────
        bar_time_is_open = cfg.strategy.bar_time_is_open if cfg else True
        if et in ("BUY_CE", "BUY_PE"):
            return TelegramAlerter._fmt_entry(
                et, event, extra, option_q, rr_ratio, strike_mode, bar_time_is_open,
            )
        elif et in ("TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE"):
            return TelegramAlerter._fmt_exit(
                et, event, extra, option_q, rr_ratio, strike_mode, bar_time_is_open,
            )
        elif et == "SQUARE_OFF":
            return TelegramAlerter._fmt_squareoff(
                event, extra, option_q, rr_ratio, strike_mode, bar_time_is_open,
            )
        elif et == "POSITION_PREMIUM" and position:
            return TelegramAlerter._fmt_position_premium(
                event, extra, position, bar_time_is_open,
            )
        else:
            return json.dumps(event.to_dict(), default=str)

    @staticmethod
    def _fmt_entry(
        et: str,
        event: SignalEvent,
        extra: dict,
        option_q: OptionQuote | None,
        rr_ratio: float,
        strike_mode: str,
        bar_time_is_open: bool = True,
    ) -> str:
        is_ce = et == "BUY_CE"
        direction = "LONG  📈" if is_ce else "SHORT  📉"
        action    = "BUY CALL (CE)" if is_ce else "BUY PUT (PE)"
        side_str  = "CE" if is_ce else "PE"

        or_high = extra.get("or_high")
        or_low  = extra.get("or_low")
        adx     = extra.get("adx")
        vwap    = extra.get("vwap")
        atr     = extra.get("atr")
        entry   = event.price
        stop    = event.stop
        target  = event.target

        # computed insights
        rr_label    = _risk_reward_label(entry, stop, target)
        breakout    = _spot_vs_or(entry, or_high, or_low, side_str)
        risk_pts    = abs(entry - stop) if stop else None
        reward_pts  = abs(target - entry) if target else None
        vwap_gap    = entry - vwap if vwap else None
        vwap_side   = ("above" if vwap_gap and vwap_gap > 0 else "below") if vwap else None

        ask = option_q.entry_ask() if option_q else None

        lines = []

        is_j_plus = extra.get("strategy") == "j_plus"
        module_tag = "J+ trap-fade  •  " if is_j_plus else ""

        # header
        if extra.get("catch_up"):
            lines.append(f"🔔 {bold(f'{et}  •  CATCH-UP ALERT')}")
            lines.append(italic("⏩ Signal fired earlier — act only if levels still valid"))
        else:
            header = f"{et}  •  {module_tag}{direction}"
            lines.append(f"🔔 {bold(header)}")

        friction = _spread_friction_lines(extra)
        if friction:
            lines.extend(friction)

        lines.append(DIVIDER)
        lines.append(
            f"⏰ Bar close: {bold(_bar_close_label(event.timestamp, bar_time_is_open=bar_time_is_open))}"
            f"  |  Action: {bold(action)}"
        )
        lines.append(DIVIDER)

        # spot levels
        lines.append(section("📍 SPOT LEVELS"))
        lines.append(f"  Entry:   {price(entry)}  ({html_escape(breakout)})")
        risk_label = bold(f"{risk_pts:.0f} pts") if risk_pts is not None else "—"
        reward_label = bold(f"{reward_pts:.0f} pts") if reward_pts is not None else "—"
        lines.append(f"  SL:      {price(stop)}  (risk {risk_label})")
        lines.append(f"  Target:  {price(target)}  (reward {reward_label})")
        lines.append(f"  R:R      {rr_label}")
        lines.extend(
            format_entry_pnl_lines(
                entry=entry,
                stop=stop,
                target=target,
                side=side_str,
                delta=option_q.delta if option_q else extra.get("option_delta"),
                theta=option_q.theta if option_q else extra.get("option_theta"),
                iv=option_q.iv if option_q else extra.get("option_iv"),
                gamma=option_q.gamma if option_q else extra.get("option_gamma"),
                atr=atr,
            )
        )

        # OR context
        lines.append("")
        lines.append(section("📊 OPENING RANGE"))
        lines.append(f"  High: {price(or_high)}  |  Low: {price(or_low)}")
        if or_high and or_low:
            lines.append(f"  Width: {bold(f'{or_high - or_low:.0f} pts')}")

        # indicators
        lines.append("")
        lines.append(section("📡 INDICATORS"))
        adx_str = f"{adx:.1f}" if adx else "—"
        adx_tag = "  ✅ Strong" if adx and adx >= 25 else ("  ⚡ Moderate" if adx and adx >= 18 else "")
        lines.append(f"  ADX:  {bold(adx_str)}{adx_tag}")
        if vwap and vwap_gap is not None:
            lines.append(
                f"  VWAP: {price(vwap)}  "
                f"(price {vwap_side}, gap {bold(f'{abs(vwap_gap):.1f} pts')})"
            )
        if atr:
            lines.append(f"  ATR:  {bold(f'{atr:.1f} pts')}  (1-candle volatility)")

        # option (manual) — exits follow spot SL/target from strategy
        lines.append("")
        lines.append(section(f"🎯 OPTION (manual)  •  {action}"))
        if option_q:
            symbol = option_q.trading_symbol or f"NIFTY {option_q.strike} {option_q.option_type}"
            if option_q.ask:
                lines.append(f"  {bold(html_escape(symbol))}  ·  Ask {bold(f'₹{option_q.ask:.2f}')}")
            else:
                lines.append(f"  {bold(html_escape(symbol))}")
            lines.append(f"  Expiry: {bold(html_escape(option_q.expiry))}")
            if option_q.bid and option_q.spread is not None:
                lines.append(
                    f"  Bid {bold(f'₹{option_q.bid:.2f}')}  |  "
                    f"Spread {bold(f'{option_q.spread:.2f}')}"
                )
            if option_q.oi:
                lines.append(f"  OI: {bold(f'{int(option_q.oi):,}')}")
            if option_q.note:
                lines.append(f"  ⚠️ {html_escape(option_q.note)}")
            lines.append(f"  {_spot_exit_plan(stop, target)}")
            # Premium lifecycle brackets (Phase 2) - compact format for entry alerts
            if ask and ask > 0:
                from bot.option_truth import compute_premium_brackets
                brackets = compute_premium_brackets(ask)
                target_pct = int(round((PREMIUM_TARGET_MULT - 1.0) * 100))
                stop_pct = int(round((1.0 - PREMIUM_STOP_MULT) * 100))
                lines.append(
                    f"  📈 Premium Target (+{target_pct}%): {bold(f'₹{brackets.target:.2f}')}  |  "
                    f"📉 Premium Stop (-{stop_pct}%): {bold(f'₹{brackets.stop:.2f}')}  |  "
                    f"R:R {brackets.rr_label}"
                )
            
            backup = _premium_backup_line(ask)
            if backup:
                lines.append(f"  {backup}")
            elif not option_q.ask:
                lines.append(italic("  Get ask from broker before entry"))
                lines.append(italic("  Backup: ~40–50% premium loss if spot SL not reached"))
        else:
            lines.append(italic("  Strike/quote unavailable — check broker app"))
            lines.append(f"  {_spot_exit_plan(stop, target)}")

        lines.append("")
        if is_j_plus:
            lines.append(f"✅ Module: {bold('J+')}  |  Filters: {html_escape(event.reason)}")
        else:
            lines.append(f"✅ Filters: {html_escape(event.reason)}")

        return "\n".join(lines)

    @staticmethod
    def _fmt_exit(
        et: str,
        event: SignalEvent,
        extra: dict,
        option_q: OptionQuote | None,
        rr_ratio: float,
        strike_mode: str,
        bar_time_is_open: bool = True,
    ) -> str:
        is_target = "TARGET" in et
        is_ce     = et.endswith("_CE")
        side_str  = "CE" if is_ce else "PE"
        trade_dir = "LONG" if is_ce else "SHORT"

        entry      = extra.get("entry")
        exit_price = event.price
        stop       = event.stop
        target     = event.target

        # P&L
        if entry is not None:
            if is_ce:
                spot_pnl = exit_price - entry
            else:
                spot_pnl = entry - exit_price
            pnl_line = pts_signed(spot_pnl)
            pnl_pct = f"({bold(f'{abs(spot_pnl)/entry*100:.2f}%')} move)"
        else:
            pnl_line = "—"
            pnl_pct = ""

        if is_target:
            header = f"🎯 {bold('TARGET HIT')}  •  {trade_dir} EXIT ✅"
            outcome = bold("PROFIT BOOKED")
        else:
            header = f"🛑 {bold('STOP HIT')}  •  {trade_dir} EXIT ❌"
            outcome = bold("LOSS — SL TRIGGERED")

        ask = option_q.entry_ask() if option_q else extra.get("option_ask")

        lines = [header]
        lines.append(DIVIDER)
        lines.append(
            f"⏰ Bar close: {bold(_bar_close_label(event.timestamp, bar_time_is_open=bar_time_is_open))}"
            f"  |  {outcome}"
        )
        lines.append(DIVIDER)

        lines.append(section("📍 SPOT P&L"))
        lines.append(f"  Entry:  {price(entry)}")
        lines.append(f"  Exit:   {price(exit_price)}")
        lines.append(f"  P&L:    {pnl_line}  {pnl_pct}")
        if stop and target:
            rr = _risk_reward_label(entry or exit_price, stop, target)
            lines.append(
                f"  R:R was: {rr}  |  SL {price(stop)} / Tgt {price(target)}"
            )

        if option_q:
            lines.append("")
            lines.append(section(f"🎯 OPTION  •  {html_escape(option_q.trading_symbol)}"))
            lines.append(f"  Expiry: {bold(html_escape(option_q.expiry))}")
            if ask:
                lines.append(f"  Entry ask was: {bold(f'₹{ask:.2f}')}")
            lines.append(
                f"  → {bold('Exit triggered by SPOT')} — book option at market in broker app"
            )

        lines.append("")
        lines.append(f"📋 Reason: {html_escape(event.reason)}")

        wick = _target_wick_postmortem_line(extra, et)
        if wick:
            lines.append("")
            lines.append(wick)

        return "\n".join(lines)

    @staticmethod
    def _fmt_position_premium(
        event: SignalEvent,
        extra: dict,
        position: Position,
        bar_time_is_open: bool = True,
    ) -> str:
        """POSITION_PREMIUM diagnostic ping (IV crush below P&L when flagged)."""
        entry = position.entry_price
        spot = event.price
        is_ce = position.side.value == "CE"
        if entry is not None:
            spot_pnl = (spot - entry) if is_ce else (entry - spot)
            pnl_line = pts_signed(spot_pnl)
        else:
            pnl_line = "—"

        lines = [f"📊 {bold('POSITION STATUS')}  •  {position.side.value}"]
        lines.append(DIVIDER)
        lines.append(
            f"⏰ Bar close: {bold(_bar_close_label(event.timestamp, bar_time_is_open=bar_time_is_open))}"
        )
        lines.append(DIVIDER)
        lines.append(section("📍 SPOT P&L"))
        lines.append(f"  Spot:   {price(spot)}")
        lines.append(f"  P&L:    {pnl_line}")

        iv_line = _iv_crush_alarm_line(
            iv_crush_bleed=bool(extra.get("iv_crush_bleed") or position.iv_crush_bleed),
            iv_at_entry=extra.get("iv_at_entry") or position.iv_at_entry,
            iv_delta=extra.get("iv_delta") if extra.get("iv_delta") is not None else position.last_iv_delta,
        )
        if iv_line:
            lines.append(iv_line)

        return "\n".join(lines)

    def chop_stop_exit(self, position: Position) -> None:
        """Chop stop exit alert (Phase 2) - advisory only, no position state change."""
        ltp = position.option_ltp or 0.0
        symbol = position.option_symbol or f"NIFTY {position.option_strike} {position.side.value}"
        
        text = (
            f"[VELOCITY CHOP STOP]: Position scratched at ₹{ltp:.2f}. "
            f"20-minute velocity momentum window elapsed before target achieved.\n\n"
            f"Strike: {html_escape(symbol)}"
        )
        
        if has_telegram_alerts(self.cfg):
            self._send_html(text)

    @staticmethod
    def format_position_status(
        position: Position,
        *,
        spot: float | None,
        spot_pnl: float | None,
    ) -> str:
        """Full POSITION_STATUS block for /position (open trade)."""
        lines = [
            f"📍 Open position — {position.side.value}",
            DIVIDER,
            f"Entry:   {position.entry_price:.2f}" if position.entry_price else "Entry:   —",
        ]
        if position.stop:
            lines.append(f"SL:      {position.stop:.2f}")
        else:
            lines.append("SL:      —")
        if position.target:
            lines.append(f"Target:  {position.target:.2f}")
        else:
            lines.append("Target:  —")
        if position.option_symbol:
            lines.append(f"Option:  {position.option_symbol}")
        if spot is not None:
            if spot_pnl is not None:
                arrow = "+" if spot_pnl > 0 else ""
                lines.append(f"Spot:    {spot:.2f}  (P&L {arrow}{spot_pnl:.1f} pts vs entry)")
            else:
                lines.append(f"Spot:    {spot:.2f}")
        
        # Add premium bracket information (Phase 2)
        premium_lines = _format_premium_brackets(position)
        if premium_lines:
            lines.extend(premium_lines)
        
        lines.extend(position_status_iv_alarm_lines(position))
        return "\n".join(lines)

    @staticmethod
    def _fmt_squareoff(
        event: SignalEvent,
        extra: dict,
        option_q: OptionQuote | None,
        rr_ratio: float,
        strike_mode: str,
        bar_time_is_open: bool = True,
    ) -> str:
        entry      = extra.get("entry")
        exit_price = event.price
        is_ce      = (event.side == "CE")

        if entry is not None:
            spot_pnl = (exit_price - entry) if is_ce else (entry - exit_price)
            pnl_line = pts_signed(spot_pnl)
        else:
            pnl_line = "—"

        ask = option_q.entry_ask() if option_q else extra.get("option_ask")

        lines = [f"⏹ {bold('SQUARE OFF')}  •  EOD EXIT"]
        lines.append(DIVIDER)
        lines.append(
            f"⏰ Bar close: {bold(_bar_close_label(event.timestamp, bar_time_is_open=bar_time_is_open))}"
            f"  |  {bold('15:15 forced exit')}"
        )
        lines.append(DIVIDER)

        lines.append(section("📍 SPOT P&L"))
        lines.append(f"  Entry: {price(entry)}  →  Exit: {price(exit_price)}")
        lines.append(f"  P&L:   {pnl_line}")

        lines.append("")
        lines.append(f"⚠️  {bold('Close your option position NOW')} in broker app")

        if option_q:
            lines.append("")
            lines.append(section(f"🎯 OPTION  •  {html_escape(option_q.trading_symbol)}"))
            lines.append(f"  Expiry: {bold(html_escape(option_q.expiry))}")
            if ask:
                lines.append(f"  Entry ask was: {bold(f'₹{ask:.2f}')}")
            lines.append(
                f"  → {bold('Sell at market')} — EOD square-off follows spot exit"
            )

        return "\n".join(lines)


class NtfyAlerter(TelegramAlerter):
    """Push alerts via ntfy.sh (plain text, same message content as Telegram)."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        topic = urllib.parse.quote(cfg.ntfy_topic, safe="")
        self._url = f"{cfg.ntfy_server.rstrip('/')}/{topic}"

    def send(self, text: str, *, parse_mode: str | None = None) -> None:
        body = html_to_plain(text) if parse_mode else text
        title = body.split("\n", 1)[0][:200]
        url = f"{self._url}?{urllib.parse.urlencode({'title': title})}"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with _urlopen(req, timeout=20) as resp:
                if resp.status >= 400:
                    detail = resp.read().decode(errors="replace")
                    raise RuntimeError(f"ntfy HTTP {resp.status}: {detail}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"ntfy HTTP {exc.code}: {detail}") from exc


class CompositeAlerter:
    """Fan out alerts to multiple backends."""

    def __init__(self, alerters: list[TelegramAlerter]) -> None:
        self._alerters = alerters

    def __getattr__(self, name: str):
        def method(*args, **kwargs):
            errors: list[Exception] = []
            for alerter in self._alerters:
                try:
                    getattr(alerter, name)(*args, **kwargs)
                except Exception as exc:
                    errors.append(exc)
            if len(errors) == len(self._alerters):
                raise errors[-1]

        return method


def create_alerter(cfg: AppConfig) -> TelegramAlerter | CompositeAlerter:
    """Build the configured alert backend(s)."""
    alerters: list[TelegramAlerter] = []
    if has_ntfy_alerts(cfg):
        alerters.append(NtfyAlerter(cfg))
    if has_telegram_alerts(cfg):
        alerters.append(TelegramAlerter(cfg))
    if not alerters:
        raise RuntimeError("No alert channel configured")
    if len(alerters) == 1:
        return alerters[0]
    return CompositeAlerter(alerters)
