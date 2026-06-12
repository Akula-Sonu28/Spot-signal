#!/usr/bin/env python3
"""End-to-end test: Upstox fetch → strategy → Telegram + CSV (today's session replay)."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from zoneinfo import ZoneInfo

from bot.alerts import TelegramAlerter
from bot.config import AUTO_TRADE, load_app_config, validate_app_config
from bot.data_feed import fetch_with_retry
from bot.indicators import or_width
from bot.logger import LiveEventLogger, ReplayLogger
from bot.state import LiveMonitorState, make_day_state
from bot.signal_enrich import enrich_event, save_option_on_position, snapshot_option
from bot.combined import process_session_bar
from bot.day_router import ensure_day_mode
from bot.strategy import build_bar_context


def main() -> int:
    cfg = load_app_config(".env")
    errors = validate_app_config(cfg)
    if errors:
        for e in errors:
            print(f"Config error: {e}")
        return 1
    if AUTO_TRADE or cfg.auto_trade:
        print("AUTO_TRADE must be False")
        return 1

    session = date.today()
    session_str = session.isoformat()
    zone = ZoneInfo(cfg.timezone)

    # Use separate state so live monitor_state.json is untouched tomorrow
    e2e_state = Path("data/live/monitor_state_e2e.json")
    e2e_log = Path("data/live/signals_e2e.csv")

    alerter = TelegramAlerter(cfg)
    event_log = LiveEventLogger(e2e_log)
    monitor = LiveMonitorState(session_date=session_str, day=make_day_state(session_str))

    alerter.send(
        "🧪 E2E test starting\n"
        f"Date: {session_str}\n"
        "Replaying today's 5m bars → strategy → Telegram\n"
        "(No orders — AUTO_TRADE=False)"
    )

    print("Fetching Upstox data...")
    snapshot = fetch_with_retry(cfg, session)
    today_df = snapshot.dataframe[snapshot.dataframe["session_date"] == session_str].copy()
    full_df = snapshot.dataframe
    print(f"  futures={snapshot.futures_key} today_bars={len(today_df)}")

    replay = monitor.to_replay_state()
    logger = ReplayLogger()
    all_events = []

    for i, row in today_df.iterrows():
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
        combined = cfg.combined
        if combined is None:
            from bot.config import load_combined_config
            combined = load_combined_config(cfg.strategy)
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
                event_log.log_system("OR_READY", f"OR {replay.day.or_high}/{replay.day.or_low}")
                monitor.or_alert_sent = True
                print(f"  OR ready: {replay.day.or_high:.2f} / {replay.day.or_low:.2f}")

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
                opt = (event.extra or {}).get("option_strike", "—")
                print(f"  {event.timestamp.strftime('%H:%M')} {event.event_type} @ {event.price:.2f} strike={opt}")

    monitor.save(e2e_state)

    summary_lines = [
        "✅ E2E test complete",
        f"Date: {session_str}",
        f"Bars: {len(today_df)}",
        f"Events: {len(all_events)}",
    ]
    for e in all_events:
        summary_lines.append(f"• {e.timestamp.strftime('%H:%M')} {event.event_type} @ {e.price:.2f}")

    if not all_events:
        summary_lines.append("No trade signals today (filters may have blocked).")

    summary_lines.append(f"Log: {e2e_log}")
    alerter.send("\n".join(summary_lines))

    print("\nE2E complete.")
    print(f"Telegram alerts sent: {1 + (1 if monitor.or_alert_sent else 0) + len(all_events)}")
    print(f"CSV: {e2e_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
