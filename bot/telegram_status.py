"""Read-only Telegram status queries and message formatters (no I/O side effects)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from bot.config import LOCKED_STRATEGY_VERSION, AppConfig, load_combined_config
from bot.early_watch import bar_close_time
from bot.scheduler import in_monitor_window, in_signal_window

if TYPE_CHECKING:
    from bot.state import LiveMonitorState

TELEGRAM_MAX_CHARS = 3800

ERROR_EVENT_TYPES = frozenset({
    "DATA_ERROR",
    "DATA_STALE",
    "SIGNALS_PAUSED",
    "RUNTIME_ERROR",
})


@dataclass(frozen=True)
class TodayContext:
    now: datetime
    session_date: str
    monitor: LiveMonitorState | None
    remote_pid: int | None
    market_pid: int | None

    @property
    def market_running(self) -> bool:
        return self.market_pid is not None


@dataclass(frozen=True)
class FeedProbeResult:
    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    bar_count: int = 0
    last_close: float | None = None
    last_bar_open: datetime | None = None
    stale: bool = False
    error: str | None = None


def truncate_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 14] + "\n…(truncated)"


def load_today_context(
    cfg: AppConfig,
    state_path: Path,
    *,
    now: datetime | None = None,
    market_pid: int | None = None,
    remote_pid: int | None = None,
) -> TodayContext:
    from bot.state import LiveMonitorState

    zone = ZoneInfo(cfg.timezone)
    now = now or datetime.now(zone)
    session_date = now.date().isoformat()
    monitor: LiveMonitorState | None = None
    if state_path.exists():
        monitor = LiveMonitorState.load(state_path, session_date)
    return TodayContext(
        now=now,
        session_date=session_date,
        monitor=monitor,
        remote_pid=remote_pid,
        market_pid=market_pid,
    )


def read_today_events(csv_path: Path, session_date: str) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("timestamp", "")
            if ts.startswith(session_date):
                rows.append(dict(row))
    return rows


def count_errors_today(csv_path: Path, session_date: str) -> int:
    return sum(
        1 for row in read_today_events(csv_path, session_date)
        if row.get("event_type") in ERROR_EVENT_TYPES
    )


def filter_trade_events(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if _is_trade_event(r.get("event_type", ""))]


def _is_trade_event(event_type: str) -> bool:
    if event_type in ("SQUARE_OFF",):
        return True
    if event_type.startswith("WATCH_"):
        return True
    if event_type.startswith(("BUY_", "TARGET_", "SL_")):
        return True
    return False


def probe_market_feed(cfg: AppConfig, now: datetime) -> FeedProbeResult:
    if not in_monitor_window(now, cfg.strategy):
        return FeedProbeResult(
            ok=False,
            skipped=True,
            skip_reason="market closed — feed probe skipped",
        )
    if not cfg.upstox_access_token:
        return FeedProbeResult(ok=False, error="UPSTOX_ACCESS_TOKEN not set")

    from bot.data_feed import fetch_with_retry, is_data_stale

    probe_cfg = AppConfig(
        upstox_access_token=cfg.upstox_access_token,
        telegram_bot_token=cfg.telegram_bot_token,
        telegram_chat_id=cfg.telegram_chat_id,
        strategy=cfg.strategy,
        combined=cfg.combined,
        max_fetch_retries=1,
        retry_backoff_sec=1.0,
        timezone=cfg.timezone,
    )
    try:
        snapshot = fetch_with_retry(probe_cfg, now.date())
    except Exception as exc:
        return FeedProbeResult(ok=False, error=str(exc))

    today = now.date().isoformat()
    today_df = snapshot.dataframe[snapshot.dataframe["session_date"] == today]
    if today_df.empty:
        return FeedProbeResult(ok=False, error="no bars for today")

    last_row = today_df.iloc[-1]
    last_open = last_row["timestamp"].to_pydatetime()
    stale = is_data_stale(snapshot, now, probe_cfg)
    return FeedProbeResult(
        ok=True,
        bar_count=len(today_df),
        last_close=float(last_row["close"]),
        last_bar_open=last_open,
        stale=stale,
    )


def next_bar_close(now: datetime, strategy) -> datetime | None:
    """Return wall-clock time of the next 5m bar close from *now* (09:15-aligned grid)."""
    if not in_monitor_window(now, strategy):
        return None
    zone = now.tzinfo or ZoneInfo(strategy.timezone)
    now_ist = now.astimezone(zone) if now.tzinfo else now.replace(tzinfo=zone)
    m = now_ist.hour * 60 + now_ist.minute
    open_m = strategy.market_open_h * 60 + strategy.market_open_m
    stop_m = strategy.monitor_stop_h * 60 + strategy.monitor_stop_m
    if m < open_m or m >= stop_m:
        return None
    offset = m - open_m
    current_bar_open_min = open_m + (offset // 5) * 5
    close_min = current_bar_open_min + 5
    if close_min > stop_m:
        return None
    bh, bm = divmod(close_min, 60)
    return now_ist.replace(hour=bh, minute=bm, second=0, microsecond=0)


def _last_bar_close_label(monitor: LiveMonitorState | None, cfg: AppConfig) -> str:
    if monitor is None or not monitor.last_processed_candle:
        return "—  (no bars processed yet)"
    try:
        lpc = datetime.fromisoformat(monitor.last_processed_candle)
        close_ts = bar_close_time(lpc, bar_time_is_open=cfg.strategy.bar_time_is_open)
        return close_ts.strftime("%H:%M IST")
    except Exception:
        return monitor.last_processed_candle


def _last_bar_age_sec(monitor: LiveMonitorState | None, cfg: AppConfig, now: datetime) -> int | None:
    if monitor is None or not monitor.last_processed_candle:
        return None
    try:
        lpc = datetime.fromisoformat(monitor.last_processed_candle)
        close_ts = bar_close_time(lpc, bar_time_is_open=cfg.strategy.bar_time_is_open)
        if close_ts.tzinfo is None:
            close_ts = close_ts.replace(tzinfo=ZoneInfo(cfg.timezone))
        return max(0, int((now - close_ts.astimezone(now.tzinfo)).total_seconds()))
    except Exception:
        return None


def format_session_window_lines(ctx: TodayContext, cfg: AppConfig) -> list[str]:
    now = ctx.now
    in_window = in_monitor_window(now, cfg.strategy)
    in_sig = in_signal_window(now, cfg.strategy)

    if ctx.market_running:
        session_status = f"🟢 RUNNING (pid {ctx.market_pid})"
    elif in_window:
        session_status = "🔴 STOPPED  ⚠️ market is open — use /start"
    else:
        session_status = "⚪ STOPPED (outside market hours)"

    if in_sig:
        window_status = "🟡 SIGNAL WINDOW OPEN"
    elif in_window:
        sig_open = f"{cfg.strategy.market_open_h:02d}:{cfg.strategy.market_open_m + cfg.strategy.or_minutes:02d}"
        window_status = f"⏳ Waiting for OR  (signals from {sig_open})"
    else:
        window_status = "💤 Market closed  (next: Mon–Fri 09:10)"

    return [f"Bot:    {session_status}", f"Window: {window_status}"]


def format_remote_line(remote_pid: int | None) -> str:
    if remote_pid is not None:
        return f"Remote: 🟢 listening (pid {remote_pid})"
    return "Remote: 🔴 not running — /commands need run_telegram_remote.ps1"


def format_monitor_detail_lines(ctx: TodayContext, cfg: AppConfig) -> list[str]:
    monitor = ctx.monitor
    if monitor is None:
        return ["State:    No data yet for today"]

    lines: list[str] = []
    lpc_str = _last_bar_close_label(monitor, cfg)
    lines.append(f"Last bar: {lpc_str} (bar close)")

    combined = cfg.combined or load_combined_config(cfg.strategy)
    day = monitor.day

    if day and day.or_defined:
        width = round((day.or_high or 0) - (day.or_low or 0), 2)
        lines.append(
            f"OR:       H {day.or_high:.2f}  /  L {day.or_low:.2f}  "
            f"(width {width:.0f} pts)"
        )
    else:
        lines.append("OR:       Not defined yet")

    if day and day.day_mode:
        mode_label = day.day_mode
        if day.day_mode == "J_PLUS" and not combined.enable_j_plus:
            mode_label = "J+ disabled (no entries)"
        lines.append(f"Mode:     {mode_label}")

    if day:
        max_trades = (
            combined.j_trap.max_trades_day
            if day.day_mode == "J_PLUS"
            else cfg.strategy.max_trades_per_day
        )
        fired = []
        if day.fired_long_today:
            fired.append("CE")
        if day.fired_short_today:
            fired.append("PE")
        fired_str = " + ".join(fired) if fired else "none"
        lines.append(f"Trades:   {day.trades_today}/{max_trades} today  ({fired_str})")

    pos = monitor.position
    from bot.state import PositionSide

    if pos.side != PositionSide.FLAT:
        opt = f"  [{pos.option_symbol}]" if pos.option_symbol else ""
        lines.append(
            f"Position: {pos.side.value}{opt}\n"
            f"          Entry {pos.entry_price:.2f}  "
            f"SL {pos.stop:.2f}  "
            f"T {pos.target:.2f}"
        )
    else:
        lines.append("Position: FLAT")

    return lines


def format_status(ctx: TodayContext, cfg: AppConfig) -> str:
    time_str = ctx.now.strftime("%d %b %Y  %H:%M IST")
    lines = [
        "📟 NIFTY Signal Engine",
        f"🕐 {time_str}",
        *format_session_window_lines(ctx, cfg),
        format_remote_line(ctx.remote_pid),
        *format_monitor_detail_lines(ctx, cfg),
    ]
    return truncate_telegram("\n".join(lines))


def format_ping(ctx: TodayContext) -> str:
    return f"Pong — remote OK, {ctx.now.strftime('%H:%M IST')}"


def format_health(
    ctx: TodayContext,
    cfg: AppConfig,
    probe: FeedProbeResult,
    csv_path: Path,
) -> str:
    time_str = ctx.now.strftime("%d %b %Y  %H:%M IST")
    lines = [
        f"🏥 Health check — {time_str}",
        "",
        format_remote_line(ctx.remote_pid),
    ]
    if ctx.market_running:
        lines.append(f"Bot:    🟢 RUNNING (pid {ctx.market_pid})")
    else:
        lines.append("Bot:    🔴 STOPPED")

    if probe.skipped:
        lines.append(f"Upstox: ⏸ {probe.skip_reason}")
    elif probe.ok:
        stale_tag = " ⚠️ STALE" if probe.stale else ""
        lines.append(f"Upstox: 🟢 OK ({probe.bar_count} bars today){stale_tag}")
        if probe.last_close is not None:
            lines.append(f"Spot:   {probe.last_close:.2f} (latest feed)")
    else:
        lines.append(f"Upstox: 🔴 {probe.error or 'unavailable'}")

    age = _last_bar_age_sec(ctx.monitor, cfg, ctx.now)
    lpc = _last_bar_close_label(ctx.monitor, cfg)
    if age is not None:
        lines.append(f"Last bar close: {lpc} ({age}s ago)")
    else:
        lines.append(f"Last bar close: {lpc}")

    errors = count_errors_today(csv_path, ctx.session_date)
    lines.append(f"Feed errors today: {errors}")

    events = filter_trade_events(read_today_events(csv_path, ctx.session_date))
    entries = sum(1 for e in events if e.get("event_type", "").startswith("BUY_"))
    exits = sum(
        1 for e in events
        if e.get("event_type", "").startswith(("TARGET_", "SL_")) or e.get("event_type") == "SQUARE_OFF"
    )
    lines.append(f"Signals today: {entries} entry, {exits} exit")

    day = ctx.monitor.day if ctx.monitor else None
    mode = day.day_mode if day else "—"
    lines.append(f"Strategy: v{LOCKED_STRATEGY_VERSION}  |  Mode: {mode or '—'}")
    return truncate_telegram("\n".join(lines))


def format_or(ctx: TodayContext, cfg: AppConfig) -> str:
    day = ctx.monitor.day if ctx.monitor else None
    if day is None or not day.or_defined or day.or_high is None or day.or_low is None:
        return "📊 Opening Range not set yet.\nWait until ~09:30 IST, or send /status."

    width = round(day.or_high - day.or_low, 2)
    combined = cfg.combined or load_combined_config(cfg.strategy)
    mode = day.day_mode or "—"

    lines = [
        "📊 Opening Range",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"OR High:  {day.or_high:.2f}",
        f"OR Low:   {day.or_low:.2f}",
        f"Width:    {width:.0f} pts",
        f"Mode:     {mode}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if mode == "SKIP":
        lines.append("No trades today — OR too narrow.")
    elif mode == "J_PLUS" and not combined.enable_j_plus:
        lines.append("Wide OR — J+ disabled (ENABLE_J_PLUS=false). No entries.")
    elif mode == "J_PLUS":
        lines.append("J+ trap-fade only. Max 1 trade after ~10:00 IST.")
        lines.append("v3.8 breakout module is OFF today.")
    else:
        lines.append(f"📈 Breakout:  above {day.or_high:.2f}  →  BUY CE")
        lines.append(f"📉 Breakdown: below {day.or_low:.2f}  →  BUY PE")
        lines.append("👀 WATCH on forming bar  |  🔔 BUY on bar close")

    lines.append("Signal window: 09:30 – 15:15 IST")
    return truncate_telegram("\n".join(lines))


def format_position(
    ctx: TodayContext,
    cfg: AppConfig,
    probe: FeedProbeResult | None = None,
) -> str:
    from bot.alerts import TelegramAlerter
    from bot.state import PositionSide

    monitor = ctx.monitor
    if monitor is None:
        return "Position: FLAT\n(No state file for today — bot may not have run yet.)"

    pos = monitor.position
    if pos.side == PositionSide.FLAT:
        return "Position: FLAT"

    spot = probe.last_close if probe and probe.ok and probe.last_close is not None else None
    spot_pnl = None
    if spot is not None and pos.entry_price is not None:
        if pos.side == PositionSide.CE:
            spot_pnl = spot - pos.entry_price
        else:
            spot_pnl = pos.entry_price - spot

    return truncate_telegram(
        TelegramAlerter.format_position_status(pos, spot=spot, spot_pnl=spot_pnl)
    )


def _format_event_line(row: dict[str, str], cfg: AppConfig) -> str:
    ts_raw = row.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_raw)
        if "T" in ts_raw:
            bar_open = ts
            close_ts = bar_close_time(bar_open, bar_time_is_open=cfg.strategy.bar_time_is_open)
            time_label = close_ts.strftime("%H:%M")
        else:
            time_label = ts.strftime("%H:%M")
    except Exception:
        time_label = ts_raw[:16]

    et = row.get("event_type", "?")
    price = row.get("price", "")
    price_str = f" @ {float(price):.2f}" if price else ""
    return f"• {time_label} {et}{price_str}"


def format_today(ctx: TodayContext, cfg: AppConfig, csv_path: Path) -> str:
    rows = filter_trade_events(read_today_events(csv_path, ctx.session_date))
    lines = [
        f"📋 Today — {ctx.session_date}",
        f"Trade events: {len(rows)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not rows:
        lines.append("No trade signals logged yet.")
        return "\n".join(lines)

    max_lines = 10
    for row in rows[-max_lines:]:
        lines.append(_format_event_line(row, cfg))
    if len(rows) > max_lines:
        lines.append(f"… and {len(rows) - max_lines} more (see signals.csv)")
    return truncate_telegram("\n".join(lines))


def format_levels(
    ctx: TodayContext,
    cfg: AppConfig,
    probe: FeedProbeResult,
) -> str:
    day = ctx.monitor.day if ctx.monitor else None
    if day is None or not day.or_defined or day.or_high is None or day.or_low is None:
        return "OR not set yet — send /or after 09:30 IST."

    lines = [
        "📏 Levels vs OR",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"OR High: {day.or_high:.2f}",
        f"OR Low:  {day.or_low:.2f}",
    ]

    if probe.skipped or not probe.ok or probe.last_close is None:
        lines.append(f"Spot:    N/A ({probe.skip_reason or probe.error or 'feed unavailable'})")
        lines.append("")
        lines.append(f"CE trigger: close above {day.or_high:.2f}")
        lines.append(f"PE trigger: close below {day.or_low:.2f}")
        return truncate_telegram("\n".join(lines))

    spot = probe.last_close
    lines.append(f"Spot:    {spot:.2f}")

    dist_ce = spot - day.or_high
    dist_pe = day.or_low - spot
    if dist_ce >= 0:
        lines.append(f"CE:      +{dist_ce:.0f} pts above OR High ✓")
    else:
        lines.append(f"CE:      {abs(dist_ce):.0f} pts below trigger ({day.or_high:.2f})")
    if dist_pe >= 0:
        lines.append(f"PE:      +{dist_pe:.0f} pts below OR Low ✓")
    else:
        lines.append(f"PE:      {abs(dist_pe):.0f} pts above trigger ({day.or_low:.2f})")

    return truncate_telegram("\n".join(lines))


def format_next(ctx: TodayContext, cfg: AppConfig) -> str:
    close_ts = next_bar_close(ctx.now, cfg.strategy)
    time_str = ctx.now.strftime("%H:%M IST")
    lines = [f"⏱ Next bar — {ctx.session_date}", f"Now: {time_str}"]

    if close_ts is None:
        lines.append("No more 5m bar closes in today's monitor window.")
        return "\n".join(lines)

    secs = max(0, int((close_ts - ctx.now).total_seconds()))
    lines.append(f"Next bar close: {close_ts.strftime('%H:%M IST')}")
    lines.append(f"Time until close: {secs}s (+ {cfg.candle_close_buffer_sec}s buffer before bot acts)")

    if in_signal_window(ctx.now, cfg.strategy):
        lines.append("Signal window: OPEN — entries allowed on confirmed bar close")
    elif in_monitor_window(ctx.now, cfg.strategy):
        lines.append("Signal window: waiting for OR / not yet in entry window")
    else:
        lines.append("Outside market hours")

    return "\n".join(lines)
