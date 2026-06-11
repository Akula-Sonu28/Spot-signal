#!/usr/bin/env python3
"""Fetch today's Upstox data, run v3.7 replay with futures VWAP, print validation report."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.config import DEFAULT_CONFIG
from bot.futures_vwap import (
    INDEX_INSTRUMENT,
    aggregate_1m_to_5m,
    fetch_historical_1m,
    fetch_intraday_5m,
    fetch_merged_today,
    merge_index_and_futures,
    merged_bars_to_dataframe,
    nearest_nifty_future_key,
)
from bot.indicators import or_width
from bot.replay import load_candles_csv, run_replay_fast

TZ = ZoneInfo("Asia/Kolkata")
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "live"


def _prior_trading_days(session: date, count: int = 3) -> list[date]:
    found: list[date] = []
    d = session - timedelta(days=1)
    while len(found) < count and (session - d).days < 14:
        if d.weekday() < 5:
            found.append(d)
        d -= timedelta(days=1)
    return list(reversed(found))


def _has_1m_data(instrument: str, day: date, token: str | None) -> bool:
    ds = day.isoformat()
    try:
        candles = fetch_historical_1m(instrument, ds, ds, token=token)
        return len(candles) > 0
    except Exception:
        return False


def build_replay_dataframe(session: date, token: str | None = None) -> tuple[str, pd.DataFrame]:
    fut_key = nearest_nifty_future_key(as_of=session)
    _, today_merged = fetch_merged_today(token=token, as_of=session)
    if not today_merged:
        raise RuntimeError("No merged intraday bars for today")

    warmup_days = _prior_trading_days(session, count=3)
    warmup_bars = []
    for day in warmup_days:
        if not _has_1m_data(INDEX_INSTRUMENT, day, token):
            continue
        ds = day.isoformat()
        idx_1m = fetch_historical_1m(INDEX_INSTRUMENT, ds, ds, token=token)
        fut_1m = fetch_historical_1m(fut_key, ds, ds, token=token)
        idx_5m = aggregate_1m_to_5m(idx_1m)
        fut_5m = aggregate_1m_to_5m(fut_1m)
        merged = merge_index_and_futures(idx_5m, fut_5m)
        warmup_bars.extend(merged)

    today_df = merged_bars_to_dataframe(today_merged)
    if warmup_bars:
        warm_df = merged_bars_to_dataframe(warmup_bars)
        df = pd.concat([warm_df, today_df], ignore_index=True)
    else:
        df = today_df

    zone = TZ
    ts = pd.to_datetime(df["timestamp"], utc=False)
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(zone)
    else:
        ts = ts.dt.tz_convert(zone)
    df["timestamp"] = ts
    df["session_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
    return fut_key, df.sort_values("timestamp").reset_index(drop=True)


def _session_summary(df: pd.DataFrame, session: str) -> dict:
    from bot.strategy import build_bar_context, update_or
    from bot.state import DayState

    day = df[df["session_date"] == session]
    cfg = DEFAULT_CONFIG
    day_state = DayState(session_date=session)
    or_h = or_l = None
    for i, row in day.iterrows():
        bar = build_bar_context(
            int(i),
            row["timestamp"].to_pydatetime(),
            session,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            0.0,
            float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            None,
            None,
            cfg,
            TZ,
        )
        update_or(day_state, bar, cfg)
    or_h, or_l = day_state.or_high, day_state.or_low
    width = or_width(or_h, or_l)
    return {
        "bars": len(day),
        "or_high": or_h,
        "or_low": or_l,
        "or_width": width,
        "or_width_ok": width is not None and DEFAULT_CONFIG.min_or_range <= width <= DEFAULT_CONFIG.max_or_range,
        "first_bar": str(day.iloc[0]["timestamp"]),
        "last_bar": str(day.iloc[-1]["timestamp"]),
        "last_close": float(day.iloc[-1]["close"]),
    }


def main() -> None:
    token = os.environ.get("UPSTOX_TOKEN")
    session = date.today()
    session_str = session.isoformat()

    print("=" * 60)
    print(f"NIFTY v3.7 — Today check ({session_str} IST)")
    print("=" * 60)

    fut_key, df = build_replay_dataframe(session, token=token)
    print(f"Futures VWAP source: {fut_key}")
    print(f"Total bars (incl. warmup): {len(df)}")
    print(f"Today bars: {len(df[df['session_date'] == session_str])}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today_only = df[df["session_date"] == session_str].copy()
    csv_path = OUT_DIR / f"{session_str}_nifty_5m_merged.csv"
    today_only.to_csv(csv_path, index=False)
    print(f"Saved: {csv_path}")

    summary = _session_summary(df, session_str)
    print("\n--- Opening Range (Pine isInOR: 9:15–9:30 bar close window) ---")
    print(f"  OR high:  {summary['or_high']:.2f}")
    print(f"  OR low:   {summary['or_low']:.2f}")
    print(f"  OR width: {summary['or_width']:.2f}  valid(25-100): {summary['or_width_ok']}")
    print(f"  Session:  {summary['first_bar']} -> {summary['last_bar']}")
    print(f"  Last close: {summary['last_close']:.2f}")

    vwap_ok = today_only["vwap"].notna().all()
    print(f"\n--- VWAP (futures, basis-adjusted) ---")
    print(f"  All today bars have VWAP: {vwap_ok}")
    if vwap_ok:
        print(f"  Last VWAP proxy: {float(today_only.iloc[-1]['vwap']):.2f}")

    logger = run_replay_fast(df)
    today_start = int(df.index[df["session_date"] == session_str].min())
    today_events = [e for e in logger.events if e.bar_index >= today_start]

    print(f"\n--- Strategy events (today only) ---")
    if not today_events:
        print("  No entries/exits today.")
    else:
        for e in today_events:
            print(
                f"  {e.timestamp.strftime('%H:%M')} | {e.event_type:12} | "
                f"price={e.price:.2f} stop={e.stop} target={e.target} | {e.reason}"
            )

    out_log = OUT_DIR / f"{session_str}_events.json"
    with out_log.open("w") as f:
        json.dump([e.to_dict() for e in today_events], f, indent=2, default=str)
    print(f"\nEvent log: {out_log}")

    # Diagnostic: why no entry if empty
    if not any(e.event_type.startswith("BUY_") for e in today_events):
        print("\n--- Diagnostic (no BUY today) ---")
        day = df[df["session_date"] == session_str].copy()
        enriched = run_replay_fast(df)  # already ran; use indicators from recompute
        from bot.replay import _compute_indicators

        day_idx = day.index.tolist()
        full = _compute_indicators(df)
        or_h, or_l = summary["or_high"], summary["or_low"]
        for i in day_idx:
            row = full.loc[i]
            ts = row["timestamp"]
            m = ts.hour * 60 + ts.minute
            if m < 9 * 60 + 30 or m >= 15 * 60 + 15:
                continue
            adx = row.get("adx")
            vwap = row.get("vwap")
            c = float(row["close"])
            if pd.isna(adx) or adx < DEFAULT_CONFIG.adx_min:
                continue
            if c > or_h and vwap is not None and not pd.isna(vwap) and c > vwap:
                print(f"  CE setup {ts.strftime('%H:%M')} close={c:.2f} adx={adx:.1f} vwap={vwap:.2f} (blocked by position/counter?)")
            if c < or_l and vwap is not None and not pd.isna(vwap) and c < vwap:
                print(f"  PE setup {ts.strftime('%H:%M')} close={c:.2f} adx={adx:.1f} vwap={vwap:.2f}")

    entries = [e for e in today_events if e.event_type in ("BUY_CE", "BUY_PE")]
    print("\n--- Verdict ---")
    if entries:
        print(f"  {len(entries)} signal(s) fired — compare marks on TradingView v3.7 chart.")
    else:
        print("  No signal today — valid if filters blocked (OR break + VWAP + ADX).")
        print("  Compare OR box and VWAP on TV; today had sustained trade below OR + below VWAP.")
    print("  Next: open NIFTY 5m TV with locked v3.7 and verify same direction.")
    print("=" * 60)


if __name__ == "__main__":
    main()
