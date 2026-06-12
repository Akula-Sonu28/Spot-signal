"""Telegram notifications for live signal monitoring."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
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


from bot.config import LOCKED_STRATEGY_VERSION, AppConfig
from bot.logger import SignalEvent
from bot.option_lookup import OptionQuote, format_option_lines, format_premium_risk_lines
from bot.state import Position


def _fmt(v: float | None, decimals: int = 2) -> str:
    return f"{v:.{decimals}f}" if v is not None else "—"


def _pts(v: float | None) -> str:
    return f"{v:.0f} pts" if v is not None else "—"


def _pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _risk_reward_label(entry: float, stop: float | None, target: float | None) -> str:
    """Compute actual R:R from spot levels."""
    if stop is None or target is None:
        return "—"
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return "—"
    rr = reward / risk
    return f"{rr:.1f}R"


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


def _pnl_spot(entry: float | None, exit_price: float, side: str) -> str:
    """Spot P&L in points."""
    if entry is None:
        return ""
    if side in ("CE", "LONG_EXIT"):
        pts = exit_price - entry
    else:
        pts = entry - exit_price
    arrow = "🟢 +" if pts > 0 else "🔴 "
    return f"{arrow}{pts:.1f} pts"


def _pnl_premium(entry_ask: float | None, sl_frac: float = 0.5,
                 rr: float = 2.0) -> tuple[str, str]:
    """Return (sl_str, target_str) for premium risk."""
    if entry_ask is None or entry_ask <= 0:
        return "—", "—"
    sl = entry_ask * (1 - sl_frac)
    target = entry_ask + (entry_ask - sl) * rr
    return f"₹{sl:.2f}", f"₹{target:.2f}"


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

    # ── System messages ───────────────────────────────────────────────────────

    def bot_started(self) -> None:
        self.send(
            f"🟢 NIFTY Signal Engine  •  LIVE  (v{LOCKED_STRATEGY_VERSION})\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Mode:       ALERTS ONLY  (no auto-trade)\n"
            "Instrument: NIFTY 50  |  5m bars  |  IST\n"
            "Session:    09:15 – 15:30\n"
            "Signals:    09:30 – 15:15  (post-OR)\n"
            "Playbook:   set after OR  (v3.8 / J+ / skip)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Waiting for Opening Range…"
        )

    def bot_stopped(self, reason: str = "shutdown") -> None:
        self.send(
            f"🔴 NIFTY Signal Engine  •  STOPPED\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reason: {reason}"
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
            mode_line = "Mode: SKIP — OR too narrow, no trades today"
            action = "No entries for this session."
        elif day_mode == "J_PLUS" and not enable_j_plus:
            mode_line = "Mode: Wide OR — J+ disabled"
            action = (
                "OR width > 100 pts but ENABLE_J_PLUS=false.\n"
                "No entries for this session."
            )
        elif day_mode == "J_PLUS":
            mode_line = "Mode: J+ trap-fade (wide OR)"
            action = (
                "Fake-break trap fade only. Max 1 trade after ~10:00 IST.\n"
                "v3.8 breakout module is OFF today."
            )
        else:
            mode_line = "Mode: v3.8 OR breakout"
            action = (
                f"📈 Breakout:  above {or_high:.2f}  →  BUY CE\n"
                f"📉 Breakdown: below {or_low:.2f}  →  BUY PE\n"
                "J+ trap module is OFF today."
            )

        self.send(
            "📊 Opening Range Set\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"OR High:  {or_high:.2f}\n"
            f"OR Low:   {or_low:.2f}\n"
            f"Width:    {width:.0f} pts\n"
            f"{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{action}\n"
            "Signal window: 09:30 – 15:15 IST"
        )

    def data_error(self, message: str, *, during_signal_hours: bool = False) -> None:
        if during_signal_hours:
            header = "🚨 Feed issue during SIGNAL HOURS"
        else:
            header = "⚠️ Data / API warning"
        self.send(
            f"{header}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{message}\n"
            "No signals generated this tick."
        )

    def signals_paused(self, minutes_without_bars: int) -> None:
        self.send(
            "🚨 Signals Paused\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"No 5m bars for {minutes_without_bars}+ min during signal window.\n"
            "⚠️ You may miss trades until the feed recovers.\n"
            "Action: check Upstox status / bot log."
        )

    def signal_event(self, event: SignalEvent, position: Position | None = None) -> None:
        self.send(self.format_signal(event, self.cfg, position))

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
        if et in ("BUY_CE", "BUY_PE"):
            return TelegramAlerter._fmt_entry(et, event, extra, option_q,
                                               rr_ratio, strike_mode)
        elif et in ("TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE"):
            return TelegramAlerter._fmt_exit(et, event, extra, option_q,
                                              rr_ratio, strike_mode)
        elif et == "SQUARE_OFF":
            return TelegramAlerter._fmt_squareoff(event, extra, option_q,
                                                   rr_ratio, strike_mode)
        else:
            return json.dumps(event.to_dict(), default=str)

    @staticmethod
    def _fmt_entry(et: str, event: SignalEvent, extra: dict,
                   option_q: OptionQuote | None,
                   rr_ratio: float, strike_mode: str) -> str:
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

        # option premium levels
        ask = option_q.entry_ask() if option_q else None
        prem_sl, prem_tgt = _pnl_premium(ask, rr=rr_ratio)

        lines = []

        is_j_plus = extra.get("strategy") == "j_plus"
        module_tag = "J+ trap-fade  •  " if is_j_plus else ""

        # header
        if extra.get("catch_up"):
            lines.append(f"🔔 {et}  •  CATCH-UP ALERT")
            lines.append("⏩ Signal fired earlier — act only if levels still valid")
        else:
            lines.append(f"🔔 {et}  •  {module_tag}{direction}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {event.timestamp.strftime('%H:%M IST')}  |  Action: {action}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # spot levels
        lines.append("📍 SPOT LEVELS")
        lines.append(f"  Entry:   {entry:.2f}  ({breakout})")
        lines.append(f"  SL:      {_fmt(stop)}  (risk {_pts(risk_pts)})")
        lines.append(f"  Target:  {_fmt(target)}  (reward {_pts(reward_pts)})")
        lines.append(f"  R:R      {rr_label}")

        # OR context
        lines.append("")
        lines.append("📊 OPENING RANGE")
        lines.append(f"  High: {_fmt(or_high)}  |  Low: {_fmt(or_low)}")
        if or_high and or_low:
            lines.append(f"  Width: {or_high - or_low:.0f} pts")

        # indicators
        lines.append("")
        lines.append("📡 INDICATORS")
        adx_str = f"{adx:.1f}" if adx else "—"
        adx_tag = "  ✅ Strong" if adx and adx >= 25 else ("  ⚡ Moderate" if adx and adx >= 18 else "")
        lines.append(f"  ADX:  {adx_str}{adx_tag}")
        if vwap and vwap_gap is not None:
            lines.append(f"  VWAP: {vwap:.2f}  (price {vwap_side}, gap {abs(vwap_gap):.1f} pts)")
        if atr:
            lines.append(f"  ATR:  {atr:.1f} pts  (1-candle volatility)")

        # option
        lines.append("")
        lines.append(f"🎯 OPTION  •  {action}")
        if option_q:
            lines.append(f"  {option_q.trading_symbol}")
            lines.append(f"  Expiry: {option_q.expiry}")
            if option_q.ask:
                lines.append(f"  Ask (buy here): ₹{option_q.ask:.2f}")
                if option_q.bid:
                    lines.append(f"  Bid: ₹{option_q.bid:.2f}  |  Spread: {_fmt(option_q.spread, 2)}")
                if option_q.ltp:
                    lines.append(f"  LTP: ₹{option_q.ltp:.2f}")
                if option_q.oi:
                    lines.append(f"  OI:  {int(option_q.oi):,}")
            else:
                lines.append("  Ask unavailable — check broker app")
            if option_q.note:
                lines.append(f"  ⚠️ {option_q.note}")

            # premium risk
            lines.append("")
            lines.append("💰 OPTION RISK  (manual execution)")
            if ask:
                lines.append(f"  Buy at ask:   ₹{ask:.2f}")
                lines.append(f"  Premium SL:   {prem_sl}  (50% loss → exit)")
                lines.append(f"  Premium Tgt:  {prem_tgt}  ({rr_ratio:.0f}R on premium)")
            else:
                lines.append("  Set SL = 50% of your buy price")
                lines.append(f"  Set Target = {rr_ratio:.0f}R on premium risk")
        else:
            lines.append("  Strike/quote unavailable — check broker app")

        lines.append("")
        if is_j_plus:
            lines.append(f"✅ Module: J+  |  Filters: {event.reason}")
        else:
            lines.append(f"✅ Filters: {event.reason}")

        return "\n".join(lines)

    @staticmethod
    def _fmt_exit(et: str, event: SignalEvent, extra: dict,
                  option_q: OptionQuote | None,
                  rr_ratio: float, strike_mode: str) -> str:
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
            pnl_pts   = f"{spot_pnl:+.1f} pts"
            pnl_arrow = "🟢" if spot_pnl > 0 else "🔴"
            pnl_pct   = f"({abs(spot_pnl)/entry*100:.2f}% move)"
        else:
            pnl_pts   = "—"
            pnl_arrow = "⚪"
            pnl_pct   = ""

        if is_target:
            header = f"🎯 TARGET HIT  •  {trade_dir} EXIT ✅"
            outcome = "PROFIT BOOKED"
        else:
            header = f"🛑 STOP HIT  •  {trade_dir} EXIT ❌"
            outcome = "LOSS — SL TRIGGERED"

        ask = option_q.entry_ask() if option_q else extra.get("option_ask")
        prem_sl, prem_tgt = _pnl_premium(ask, rr=rr_ratio)

        lines = [header]
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {event.timestamp.strftime('%H:%M IST')}  |  {outcome}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

        lines.append("📍 SPOT P&L")
        lines.append(f"  Entry:  {_fmt(entry)}")
        lines.append(f"  Exit:   {exit_price:.2f}")
        lines.append(f"  P&L:    {pnl_arrow} {pnl_pts}  {pnl_pct}")
        if stop and target:
            rr = _risk_reward_label(entry or exit_price, stop, target)
            lines.append(f"  R:R was: {rr}  |  SL {_fmt(stop)} / Tgt {_fmt(target)}")

        if option_q:
            lines.append("")
            lines.append(f"🎯 OPTION  •  {option_q.trading_symbol}")
            lines.append(f"  Expiry: {option_q.expiry}")
            if ask:
                lines.append(f"  Entry ask was: ₹{ask:.2f}")
                lines.append(f"  SL level was:  {prem_sl}  |  Target was: {prem_tgt}")
                lines.append("  → Check current LTP in broker app to book P&L")
            else:
                lines.append("  → Check current LTP in broker app to book P&L")

        lines.append("")
        lines.append(f"📋 Reason: {event.reason}")

        return "\n".join(lines)

    @staticmethod
    def _fmt_squareoff(event: SignalEvent, extra: dict,
                       option_q: OptionQuote | None,
                       rr_ratio: float, strike_mode: str) -> str:
        entry      = extra.get("entry")
        exit_price = event.price
        is_ce      = (event.side == "CE")

        if entry is not None:
            spot_pnl  = (exit_price - entry) if is_ce else (entry - exit_price)
            pnl_pts   = f"{spot_pnl:+.1f} pts"
            pnl_arrow = "🟢" if spot_pnl > 0 else "🔴"
        else:
            pnl_pts   = "—"
            pnl_arrow = "⚪"

        ask = option_q.entry_ask() if option_q else extra.get("option_ask")
        prem_sl, prem_tgt = _pnl_premium(ask, rr=rr_ratio)

        lines = ["⏹ SQUARE OFF  •  EOD EXIT"]
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {event.timestamp.strftime('%H:%M IST')}  |  15:15 forced exit")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

        lines.append("📍 SPOT P&L")
        lines.append(f"  Entry: {_fmt(entry)}  →  Exit: {exit_price:.2f}")
        lines.append(f"  P&L:   {pnl_arrow} {pnl_pts}")

        lines.append("")
        lines.append("⚠️  Close your option position NOW in broker app")

        if option_q:
            lines.append("")
            lines.append(f"🎯 OPTION  •  {option_q.trading_symbol}")
            lines.append(f"  Expiry: {option_q.expiry}")
            if ask:
                lines.append(f"  Entry ask was: ₹{ask:.2f}")
                lines.append(f"  SL ref: {prem_sl}  |  Target ref: {prem_tgt}")
            lines.append("  → Sell at current market price immediately")

        return "\n".join(lines)
