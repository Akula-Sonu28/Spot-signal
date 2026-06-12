#!/usr/bin/env python3
"""Replay one or more sessions bar-by-bar → v3.9 strategy → Telegram (isolated from live state)."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter
from bot.combined import process_session_bar
from bot.config import AUTO_TRADE, load_app_config, load_combined_config, validate_app_config
from bot.data_feed import DataFeedError, fetch_session_dataframe, fetch_with_retry
from bot.futures_vwap import (
    INDEX_INSTRUMENT,
    aggregate_1m_to_5m,
    fetch_historical_1m,
    merge_index_and_futures,
    merged_bars_to_dataframe,
    nearest_nifty_future_key,
)
from bot.indicators import or_width
from bot.logger import LiveEventLogger, ReplayLogger
from bot.replay import _compute_indicators, load_candles_csv
from bot.state import LiveMonitorState, make_day_state
from bot.day_router import ensure_day_mode
from bot.signal_enrich import enrich_event, save_option_on_position, snapshot_option
from bot.strategy import build_bar_context

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
LIVE = ROOT / "data" / "live"


def _prior_weekdays(session: date, count: int) -> list[date]:
    found: list[date] = []
    d = session - timedelta(days=1)
    while len(found) < count and (session - d).days < 21:
        if d.weekday() < 5:
            found.append(d)
        d -= timedelta(days=1)
    return list(reversed(found))


def _load_from_csv(session: date, warmup_days: int = 3) -> pd.DataFrame | None:
    """Load session + warmup from data/historical/*_5m.csv if present."""
    days = _prior_weekdays(session, warmup_days) + [session]
    frames: list[pd.DataFrame] = []
    for d in days:
        p = HIST / f"{d.isoformat()}_5m.csv"
        if not p.exists():
            p = LIVE / f"{d.isoformat()}_nifty_5m.csv"
        if p.exists():
            frames.append(load_candles_csv(p))
    if not any(str(session) in f["session_date"].values for f in frames):
        target = HIST / f"{session.isoformat()}_5m.csv"
        if not target.exists():
            return None
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    return _compute_indicators(df.reset_index(drop=True))


def _has_1m(instrument: str, day: date, token: str) -> bool:
    try:
        return len(fetch_historical_1m(instrument, day.isoformat(), day.isoformat(), token=token)) > 0
    except Exception:
        return False


def _fetch_session(cfg, session: date) -> tuple[str, pd.DataFrame]:
    """Upstox: historical 1m for past days, intraday path for today."""
    today = date.today()
    if session < today:
        fut_key = nearest_nifty_future_key(as_of=session)
        if not _has_1m(INDEX_INSTRUMENT, session, cfg.upstox_access_token):
            raise DataFeedError(f"No historical 1m data for {session.isoformat()}")
        ds = session.isoformat()
        idx_1m = fetch_historical_1m(INDEX_INSTRUMENT, ds, ds, token=cfg.upstox_access_token)
        fut_1m = fetch_historical_1m(fut_key, ds, ds, token=cfg.upstox_access_token)
        merged = merge_index_and_futures(aggregate_1m_to_5m(idx_1m), aggregate_1m_to_5m(fut_1m))
        if not merged:
            raise DataFeedError(f"No merged bars for {session.isoformat()}")
        warmup_bars = []
        for day in _prior_weekdays(session, cfg.warmup_days):
            if not _has_1m(INDEX_INSTRUMENT, day, cfg.upstox_access_token):
                continue
            dds = day.isoformat()
            wi = fetch_historical_1m(INDEX_INSTRUMENT, dds, dds, token=cfg.upstox_access_token)
            wf = fetch_historical_1m(fut_key, dds, dds, token=cfg.upstox_access_token)
            warmup_bars.extend(merge_index_and_futures(aggregate_1m_to_5m(wi), aggregate_1m_to_5m(wf)))
        today_df = merged_bars_to_dataframe(merged)
        df = (
            pd.concat([merged_bars_to_dataframe(warmup_bars), today_df], ignore_index=True)
            if warmup_bars
            else today_df
        )
        zone = ZoneInfo(cfg.timezone)
        ts = pd.to_datetime(df["timestamp"], utc=False)
        df["timestamp"] = ts.dt.tz_localize(zone) if ts.dt.tz is None else ts.dt.tz_convert(zone)
        df["session_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
        return fut_key, _compute_indicators(df.sort_values("timestamp").reset_index(drop=True))

    fut_key, df = fetch_session_dataframe(session, cfg.upstox_access_token, cfg.warmup_days)
    return fut_key, df


def replay_session(
    session: date,
    cfg,
    alerter: TelegramAlerter,
    *,
    data_source: str = "auto",
) -> int:
    session_str = session.isoformat()
    zone = ZoneInfo(cfg.timezone)
    e2e_state = LIVE / f"monitor_state_replay_{session_str}.json"
    e2e_log = LIVE / f"signals_replay_{session_str}.csv"

    combined = cfg.combined or load_combined_config(cfg.strategy)
    monitor = LiveMonitorState(session_date=session_str, day=make_day_state(session_str))
    event_log = LiveEventLogger(e2e_log)
    replay = monitor.to_replay_state()
    logger = ReplayLogger()
    all_events = []

    alerter.send(
        "🧪 Session replay → Telegram\n"
        f"Date: {session_str}\n"
        "v3.9 combined router (no orders)"
    )

    fut_key = "csv"
    df: pd.DataFrame | None = None
    if data_source in ("auto", "csv"):
        df = _load_from_csv(session, cfg.warmup_days)
        if df is not None:
            fut_key = "historical_csv"
    if df is None and data_source in ("auto", "upstox"):
        try:
            fut_key, df = _fetch_session(cfg, session)
        except DataFeedError as exc:
            alerter.send(f"❌ Replay failed {session_str}\n{exc}")
            print(f"ERROR {session_str}: {exc}")
            return 1
    if df is None:
        alerter.send(f"❌ No data for {session_str} (no CSV, Upstox failed)")
        return 1

    session_df = df[df["session_date"] == session_str].copy()
    if session_df.empty:
        alerter.send(f"❌ No bars for session {session_str}")
        return 1

    print(f"\n=== {session_str} source={fut_key} bars={len(session_df)} ===")

    for i, row in session_df.iterrows():
        bar = build_bar_context(
            index=int(i),
            timestamp=row["timestamp"].to_pydatetime(),
            session_date=session_str,
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            vol=float(row.get("volume", 0)),
            vwap=float(row["vwap"]) if pd.notna(row.get("vwap")) else None,
            atr=float(row["atr"]) if pd.notna(row.get("atr")) else None,
            adx=float(row["adx"]) if pd.notna(row.get("adx")) else None,
            cfg=cfg.strategy,
            tz=zone,
        )
        option_snap = snapshot_option(replay.position)
        before = len(logger.events)
        process_session_bar(replay, bar, logger, combined)
        after = len(logger.events)
        monitor.sync_from_replay(replay)
        monitor.last_processed_candle = bar.timestamp.isoformat()

        if replay.day and replay.day.or_defined and not monitor.or_alert_sent:
            w = or_width(replay.day.or_high, replay.day.or_low)
            if w is not None:
                mode = ensure_day_mode(replay.day, cfg.strategy)
                mode_str = mode.value if mode is not None else "V38"
                alerter.or_ready(
                    replay.day.or_high or 0,
                    replay.day.or_low or 0,
                    w,
                    day_mode=mode_str,
                    enable_j_plus=combined.enable_j_plus,
                )
                event_log.log_system("OR_READY", f"OR {replay.day.or_high}/{replay.day.or_low} mode={mode_str}")
                monitor.or_alert_sent = True
                print(f"  OR ready: width={w:.1f} mode={mode_str}")

        if after > before:
            for event in logger.events[before:after]:
                extra = dict(event.extra)
                extra.setdefault("atr", bar.atr)
                if event.event_type in ("TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE", "SQUARE_OFF"):
                    extra.update(option_snap)
                event.extra = extra
                enrich_event(event, cfg, replay.position)
                if event.event_type in ("BUY_CE", "BUY_PE"):
                    save_option_on_position(replay.position, event)
                event_log.append(event)
                alerter.signal_event(event, replay.position)
                all_events.append(event)
                strat = (event.extra or {}).get("strategy", "—")
                print(f"  {event.timestamp.strftime('%H:%M')} {event.event_type} @ {event.price:.2f} [{strat}]")

    monitor.save(e2e_state)

    summary = [
        f"✅ Replay complete — {session_str}",
        f"Bars: {len(session_df)}",
        f"Mode: {replay.day.day_mode if replay.day else '?'}",
        f"Signals: {len(all_events)}",
    ]
    for e in all_events:
        summary.append(f"• {e.timestamp.strftime('%H:%M')} {e.event_type} @ {e.price:.2f}")
    if not all_events:
        summary.append("No trade signals (filters / day mode may have blocked).")
    summary.append(f"Log: {e2e_log}")
    alerter.send("\n".join(summary))

    tele_count = (1 if monitor.or_alert_sent else 0) + len(all_events) + 2
    print(f"  Telegram messages (~): {tele_count}")
    print(f"  CSV: {e2e_log}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay session(s) with Telegram alerts")
    parser.add_argument("--date", action="append", required=True, help="YYYY-MM-DD (repeatable)")
    parser.add_argument(
        "--source",
        choices=("auto", "csv", "upstox"),
        default="auto",
        help="Data source: auto tries CSV then Upstox",
    )
    args = parser.parse_args()

    cfg = load_app_config(".env")
    errors = validate_app_config(cfg)
    if errors:
        for e in errors:
            print(f"Config error: {e}")
        return 1
    if AUTO_TRADE or cfg.auto_trade:
        print("AUTO_TRADE must be False")
        return 1

    alerter = TelegramAlerter(cfg)
    rc = 0
    for ds in args.date:
        session = date.fromisoformat(ds)
        if replay_session(session, cfg, alerter, data_source=args.source) != 0:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
