"""Market-hours scheduler for 5m bar-close signal processing."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.alerts import CompositeAlerter, NtfyAlerter, TelegramAlerter
from bot.combined import process_session_bar
from bot.config import AUTO_TRADE, AppConfig, StrategyConfig
from bot.day_router import ensure_day_mode
from bot.data_feed import DataFeedError, FeedSnapshot, fetch_with_retry, is_bar_complete, is_data_stale
from bot.early_watch import detect_early_or_watch, early_watch_key
from bot.indicators import or_width
from bot.logger import LiveEventLogger, ReplayLogger, SignalEvent
from bot.state import LiveMonitorState, Position, make_day_state
from bot.signal_enrich import enrich_event, save_option_on_position, snapshot_option
from bot.strategy import BarContext, build_bar_context

DATA_WARNING_THROTTLE_MIN = 15
SIGNALS_PAUSED_AFTER_MIN = 15

TRADE_ALERT_TYPES = frozenset({
    "BUY_CE",
    "BUY_PE",
    "SL_CE",
    "SL_PE",
    "TARGET_CE",
    "TARGET_PE",
    "SQUARE_OFF",
})


def alert_dispatch_key(event: SignalEvent) -> str:
    """Stable id for deduplicating Telegram trade alerts within a session."""
    session = event.timestamp.strftime("%Y-%m-%d")
    return f"{session}:{event.event_type}:{event.bar_index}"


def in_monitor_window(now: datetime, cfg: StrategyConfig) -> bool:
    """True during live polling hours (market open through monitor stop)."""
    m = now.hour * 60 + now.minute
    open_m = cfg.market_open_h * 60 + cfg.market_open_m
    stop_m = cfg.monitor_stop_h * 60 + cfg.monitor_stop_m
    return open_m <= m <= stop_m


def after_monitor_close(now: datetime, cfg: StrategyConfig) -> bool:
    """True after today's session end (15:30 IST) — bot should not start."""
    m = now.hour * 60 + now.minute
    stop_m = cfg.monitor_stop_h * 60 + cfg.monitor_stop_m
    return m > stop_m


def in_signal_window(now: datetime, cfg: StrategyConfig) -> bool:
    """True during the entry/signal window (after OR, before square-off)."""
    m = now.hour * 60 + now.minute
    open_m = cfg.market_open_h * 60 + cfg.market_open_m
    or_end = open_m + cfg.or_minutes
    square_off = cfg.square_off_h * 60 + cfg.square_off_m
    return or_end <= m < square_off


def can_manual_start_session(now: datetime, cfg: StrategyConfig) -> bool:
    """True when /start may launch the market session (from 09:10 IST through monitor stop)."""
    if after_monitor_close(now, cfg):
        return False
    m = now.hour * 60 + now.minute
    prep_start = cfg.market_open_h * 60 + cfg.market_open_m - 5
    stop_m = cfg.monitor_stop_h * 60 + cfg.monitor_stop_m
    return prep_start <= m <= stop_m


def should_send_data_warning(
    last_warning_at: datetime | None,
    now: datetime,
    *,
    throttle_minutes: int = DATA_WARNING_THROTTLE_MIN,
) -> bool:
    if last_warning_at is None:
        return True
    return (now - last_warning_at).total_seconds() >= throttle_minutes * 60


def should_send_signals_paused_alert(
    entry_window_seen_without_bars: datetime | None,
    last_processed_candle: str | None,
    now: datetime,
    already_sent: bool,
    *,
    pause_minutes: int = SIGNALS_PAUSED_AFTER_MIN,
) -> bool:
    if already_sent or last_processed_candle is not None:
        return False
    if entry_window_seen_without_bars is None:
        return False
    return (now - entry_window_seen_without_bars).total_seconds() >= pause_minutes * 60


class LiveScheduler:
    def __init__(
        self,
        cfg: AppConfig,
        alerter: TelegramAlerter | NtfyAlerter | CompositeAlerter,
        event_log: LiveEventLogger,
    ) -> None:
        self.cfg = cfg
        self.alerter = alerter
        self.event_log = event_log
        self.zone = ZoneInfo(cfg.timezone)
        self._running = False

    def _now(self) -> datetime:
        return datetime.now(self.zone)

    def _minutes(self, dt: datetime) -> int:
        return dt.hour * 60 + dt.minute

    def in_monitor_window(self, now: datetime | None = None) -> bool:
        return in_monitor_window(now or self._now(), self.cfg.strategy)

    def should_stop(self, now: datetime | None = None) -> bool:
        now = now or self._now()
        m = self._minutes(now)
        stop_m = self.cfg.strategy.monitor_stop_h * 60 + self.cfg.strategy.monitor_stop_m
        return m > stop_m

    def _load_state(self, session_date: str) -> LiveMonitorState:
        return LiveMonitorState.load(self.cfg.state_file, session_date)

    def _dispatch_events(self, events: list[SignalEvent], replay_position: Position) -> None:
        for event in events:
            self.event_log.append(event)
            self.alerter.signal_event(event, replay_position)

    def _prepare_trade_events(
        self,
        raw_events: list[SignalEvent],
        *,
        bar: BarContext,
        option_snap: dict,
        replay_position: Position,
        catch_up: bool,
    ) -> list[SignalEvent]:
        prepared: list[SignalEvent] = []
        for event in raw_events:
            if event.event_type not in TRADE_ALERT_TYPES:
                continue
            extra = dict(event.extra or {})
            extra.setdefault("atr", bar.atr)
            if catch_up:
                extra["catch_up"] = True
            if event.event_type in ("TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE", "SQUARE_OFF"):
                extra.update(option_snap)
            event.extra = extra
            enrich_event(event, self.cfg, replay_position)
            if event.event_type in ("BUY_CE", "BUY_PE"):
                save_option_on_position(replay_position, event)
            prepared.append(event)
        return prepared

    def _queue_trade_alerts(
        self,
        monitor: LiveMonitorState,
        events: list[SignalEvent],
        out: list[SignalEvent],
    ) -> int:
        queued = 0
        for event in events:
            key = alert_dispatch_key(event)
            if key in monitor.dispatched_alert_keys:
                continue
            monitor.dispatched_alert_keys.append(key)
            out.append(event)
            queued += 1
        return queued

    def _parse_state_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.zone)
        return dt.astimezone(self.zone)

    def _maybe_alert_data_issue(self, monitor: LiveMonitorState, msg: str, now: datetime) -> None:
        if not in_signal_window(now, self.cfg.strategy):
            return
        last_warning = self._parse_state_dt(monitor.last_data_warning_at)
        if not should_send_data_warning(last_warning, now):
            return
        self.alerter.data_error(msg, during_signal_hours=True)
        monitor.last_data_warning_at = now.isoformat()

    def _track_signals_paused(self, monitor: LiveMonitorState, now: datetime, bars_processed: int) -> None:
        if not in_signal_window(now, self.cfg.strategy):
            return

        if bars_processed > 0 or monitor.last_processed_candle is not None:
            monitor.entry_window_seen_without_bars = None
            monitor.signals_paused_alert_sent = False
            return

        if monitor.entry_window_seen_without_bars is None:
            monitor.entry_window_seen_without_bars = now.isoformat()

        seen_at = self._parse_state_dt(monitor.entry_window_seen_without_bars)
        if not should_send_signals_paused_alert(
            seen_at,
            monitor.last_processed_candle,
            now,
            monitor.signals_paused_alert_sent,
        ):
            return

        self.alerter.signals_paused(SIGNALS_PAUSED_AFTER_MIN)
        monitor.signals_paused_alert_sent = True
        self.event_log.log_system(
            "SIGNALS_PAUSED",
            f"No bars processed for {SIGNALS_PAUSED_AFTER_MIN}+ min during signal window",
        )

    def _maybe_early_or_watch(
        self,
        monitor: LiveMonitorState,
        snapshot: FeedSnapshot,
        now: datetime,
        session_date: str,
        *,
        catch_up_mode: bool,
    ) -> None:
        if not self.cfg.enable_early_or_watch or catch_up_mode:
            return
        if not in_signal_window(now, self.cfg.strategy):
            return
        if monitor.day is None or not monitor.day.or_defined:
            return
        if monitor.position.side.value != "FLAT":
            return

        today_df = snapshot.dataframe[snapshot.dataframe["session_date"] == session_date]
        if today_df.empty:
            return

        row = snapshot.dataframe.loc[today_df.index[-1]]
        bar_open = row["timestamp"].to_pydatetime()
        if is_bar_complete(
            bar_open,
            now,
            bar_time_is_open=self.cfg.strategy.bar_time_is_open,
            buffer_sec=self.cfg.candle_close_buffer_sec,
        ):
            return

        vwap = float(row["vwap"]) if pd.notna(row.get("vwap")) else None
        adx = float(row["adx"]) if pd.notna(row.get("adx")) else None
        watch = detect_early_or_watch(
            monitor.day,
            monitor.position,
            float(row["close"]),
            vwap,
            adx,
            self.cfg.strategy,
        )
        if watch is None:
            return

        key = early_watch_key(session_date, watch, bar_open)
        if key in monitor.dispatched_early_watch_keys:
            return

        monitor.dispatched_early_watch_keys.append(key)
        self.alerter.or_break_pending(
            watch,
            float(row["close"]),
            monitor.day.or_high or 0.0,
            monitor.day.or_low or 0.0,
            bar_open,
            now=now,
            vwap=vwap,
            adx=adx,
            bar_time_is_open=self.cfg.strategy.bar_time_is_open,
        )
        self.event_log.log_system(
            watch,
            f"Forming bar {bar_open.isoformat()} spot={float(row['close']):.2f}",
        )

    def process_tick(self) -> None:
        if AUTO_TRADE or self.cfg.auto_trade:
            raise RuntimeError("AUTO_TRADE must remain False")

        now = self._now()
        if not self.in_monitor_window(now):
            return

        session_date = now.date().isoformat()
        monitor = self._load_state(session_date)
        if monitor.day is None:
            monitor.day = make_day_state(session_date)

        bars_processed = 0

        try:
            snapshot = fetch_with_retry(self.cfg, now.date())
        except DataFeedError as exc:
            msg = str(exc)
            self.event_log.log_system("DATA_ERROR", msg)
            self._maybe_alert_data_issue(monitor, msg, now)
            self._track_signals_paused(monitor, now, bars_processed)
            monitor.save(self.cfg.state_file)
            return

        if is_data_stale(snapshot, now, self.cfg):
            msg = "Candle feed appears stale; skipping signal generation"
            self.event_log.log_system("DATA_STALE", msg)
            self._maybe_alert_data_issue(monitor, msg, now)
            self._track_signals_paused(monitor, now, bars_processed)
            monitor.save(self.cfg.state_file)
            return

        df = snapshot.dataframe
        today_mask = df["session_date"] == session_date
        replay = monitor.to_replay_state()
        logger = ReplayLogger()
        new_events: list[SignalEvent] = []
        catch_up_mode = monitor.last_processed_candle is None
        catch_up_alerts = 0

        for i, row in df[today_mask].iterrows():
            bar_open = row["timestamp"].to_pydatetime()
            bar_key = bar_open.isoformat()

            if monitor.last_processed_candle and bar_key <= monitor.last_processed_candle:
                continue

            if not is_bar_complete(
                bar_open,
                now,
                bar_time_is_open=self.cfg.strategy.bar_time_is_open,
                buffer_sec=self.cfg.candle_close_buffer_sec,
            ):
                continue

            bar = build_bar_context(
                index=int(i),
                timestamp=bar_open,
                session_date=session_date,
                o=float(row["open"]),
                h=float(row["high"]),
                l=float(row["low"]),
                c=float(row["close"]),
                vol=float(row.get("volume", 0)),
                vwap=float(row["vwap"]) if pd.notna(row.get("vwap")) else None,
                atr=float(row["atr"]) if pd.notna(row.get("atr")) else None,
                adx=float(row["adx"]) if pd.notna(row.get("adx")) else None,
                cfg=self.cfg.strategy,
                tz=self.zone,
            )

            option_snap = snapshot_option(replay.position)
            before = len(logger.events)
            combined = self.cfg.combined
            if combined is None:
                from bot.config import load_combined_config
                combined = load_combined_config(self.cfg.strategy)
            process_session_bar(replay, bar, logger, combined)
            after = len(logger.events)
            if after > before:
                prepared = self._prepare_trade_events(
                    logger.events[before:after],
                    bar=bar,
                    option_snap=option_snap,
                    replay_position=replay.position,
                    catch_up=catch_up_mode,
                )
                catch_up_alerts += self._queue_trade_alerts(monitor, prepared, new_events)

            monitor.sync_from_replay(replay)
            monitor.last_processed_candle = bar_key
            bars_processed += 1

            if monitor.day and monitor.day.or_defined and not monitor.or_alert_sent:
                width = or_width(monitor.day.or_high, monitor.day.or_low)
                if width is not None:
                    mode = ensure_day_mode(monitor.day, self.cfg.strategy)
                    mode_str = mode.value if mode is not None else "UNKNOWN"
                    self.alerter.or_ready(
                        monitor.day.or_high or 0,
                        monitor.day.or_low or 0,
                        width,
                        day_mode=mode_str,
                        enable_j_plus=combined.enable_j_plus,
                    )
                    monitor.or_alert_sent = True
                    self.event_log.log_system(
                        "OR_READY",
                        f"OR {monitor.day.or_high}/{monitor.day.or_low} width={width:.2f} mode={mode_str}",
                    )

        self._maybe_early_or_watch(
            monitor, snapshot, now, session_date, catch_up_mode=catch_up_mode,
        )

        self._track_signals_paused(monitor, now, bars_processed)
        monitor.save(self.cfg.state_file)
        if catch_up_mode:
            detail = f"through {monitor.last_processed_candle}"
            if catch_up_alerts:
                detail += f", {catch_up_alerts} trade alert(s) sent"
            self.event_log.log_system("CATCH_UP", f"Catch-up complete {detail}")
        if new_events:
            self._dispatch_events(new_events, replay.position)

    def _sleep_until_next_poll(self) -> None:
        """Sleep until poll interval elapses, waking early when /tick is requested."""
        from bot.telegram_commands import TelegramCommandHandler

        deadline = time.monotonic() + self.cfg.poll_interval_sec
        while time.monotonic() < deadline:
            if TelegramCommandHandler.force_tick_requested():
                return
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def run_forever(self) -> None:
        from bot.telegram_commands import TelegramCommandHandler

        self._running = True
        while self._running:
            if self.should_stop(self._now()):
                break
            force_tick = TelegramCommandHandler.force_tick_requested()
            try:
                self.process_tick()
            except Exception as exc:
                msg = f"Unexpected error: {exc}"
                self.event_log.log_system("RUNTIME_ERROR", msg)
                self.alerter.data_error(msg)
            finally:
                if force_tick:
                    TelegramCommandHandler.clear_force_tick()
            self._sleep_until_next_poll()

    def stop(self) -> None:
        self._running = False
