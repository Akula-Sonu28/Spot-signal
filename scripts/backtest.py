#!/usr/bin/env python3
"""Backtest v3.9 combined strategy on Upstox historical data or local CSV."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.backtest import format_report, run_backtest
from bot.config import CombinedStrategyConfig, DEFAULT_CONFIG, load_app_config, load_combined_config
from bot.historical_data import build_backtest_dataframe, trading_days_back
from bot.replay import load_candles_csv, run_replay_fast

ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = ROOT / "data" / "live"
MOCK_DIR = ROOT / "data" / "mock"
OUT_DIR = ROOT / "data" / "backtest"


def _load_env() -> str | None:
    try:
        cfg = load_app_config(ROOT / ".env")
        return cfg.upstox_access_token
    except Exception:
        return os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_TOKEN")


def _today_csv_fallback() -> Path | None:
    path = LIVE_DIR / f"{date.today().isoformat()}_nifty_5m_merged.csv"
    return path if path.exists() else None


def _combined_cfg(v38_only: bool) -> CombinedStrategyConfig:
    base = load_combined_config(DEFAULT_CONFIG)
    if v38_only:
        return CombinedStrategyConfig(
            strategy=base.strategy,
            enable_j_plus=False,
            j_trap=base.j_trap,
        )
    return base


def _run_on_dataframe(
    df,
    session_dates: list[str] | None,
    title: str,
    combined_cfg: CombinedStrategyConfig,
) -> int:
    logger = run_replay_fast(df, combined_cfg=combined_cfg)
    summary = run_backtest(logger, session_dates=session_dates)
    print(format_report(summary, title=title))
    return 0 if summary.total_trades >= 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest NIFTY v3.9 combined on available candle data")
    parser.add_argument(
        "--v38-only",
        action="store_true",
        help="Disable J+ (v3.8 valid-OR days only; wide OR days skip)",
    )
    parser.add_argument("--days", type=int, default=15, help="Weekdays to backtest ending today")
    parser.add_argument("--from", dest="from_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--csv", type=Path, help="Single merged CSV (timestamp,OHLC,volume[,vwap])")
    parser.add_argument("--mock", action="store_true", help="Run on all data/mock/*.csv fixtures")
    parser.add_argument("--no-cache", action="store_true", help="Refetch Upstox data, ignore cache")
    parser.add_argument("--out", type=Path, help="Write JSON summary to path")
    args = parser.parse_args()
    combined = _combined_cfg(args.v38_only)
    mode_label = "v3.8-only" if args.v38_only else "v3.9 combined"

    if args.mock:
        all_trades = []
        for path in sorted(MOCK_DIR.glob("*.csv")):
            df = load_candles_csv(path)
            logger = run_replay_fast(df, combined_cfg=combined)
            sessions = sorted(df["session_date"].unique())
            summary = run_backtest(logger, session_dates=sessions)
            print(format_report(summary, title=f"Mock: {path.name}"))
            all_trades.extend(summary.trades)
        print(f"\nMock fixtures total trades: {len(all_trades)}")
        return 0

    if args.csv:
        df = load_candles_csv(args.csv)
        sessions = sorted(df["session_date"].unique())
        return _run_on_dataframe(df, sessions, title=f"CSV: {args.csv.name} ({mode_label})", combined_cfg=combined)

    token = _load_env()
    if args.from_date:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date) if args.to_date else date.today()
        days: list[date] = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d = date.fromordinal(d.toordinal() + 1)
    else:
        end = date.fromisoformat(args.to_date) if args.to_date else date.today()
        days = trading_days_back(end, args.days)

    print(f"Fetching {len(days)} sessions: {days[0]} → {days[-1]} ...")
    try:
        df, loaded, skipped, vwap_counts = build_backtest_dataframe(
            days,
            token=token,
            warmup_days=3,
            use_cache=not args.no_cache,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Today may be missing from historical API — use saved intraday CSV
    today_str = date.today().isoformat()
    if today_str in [d.isoformat() for d in days] and today_str not in loaded:
        fallback = _today_csv_fallback()
        if fallback:
            today_df = load_candles_csv(fallback)
            warm = df[df["session_date"] < today_str]
            df = __import__("pandas").concat([warm, today_df], ignore_index=True)
            loaded.append(today_str)
            if today_str in skipped:
                skipped.remove(today_str)
            print(f"  + today from {fallback.name}")

    print(f"Loaded {len(loaded)} sessions, {len(df)} bars (incl. warmup)")
    if vwap_counts:
        parts = [f"{k}={v}" for k, v in sorted(vwap_counts.items()) if v]
        print(f"VWAP source: {', '.join(parts)}")
        if vwap_counts.get("twap_proxy", 0):
            print("  (twap_proxy = index TWAP when historical futures unavailable)")
    if skipped:
        print(f"Skipped (no data): {', '.join(skipped)}")

    title = f"Backtest {loaded[0]} → {loaded[-1]} ({len(loaded)} sessions, {mode_label})"
    logger = run_replay_fast(df, combined_cfg=combined)
    summary = run_backtest(logger, session_dates=loaded)
    print(format_report(summary, title=title))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.out or OUT_DIR / f"summary_{loaded[0]}_{loaded[-1]}_{stamp}.json"
    out_path.write_text(json.dumps(summary.to_dict(), indent=2))
    print(f"\nJSON: {out_path}")

    events_path = OUT_DIR / f"trades_{loaded[0]}_{loaded[-1]}_{stamp}.csv"
    if summary.trades:
        import csv

        with events_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(summary.trades[0].to_dict().keys()))
            writer.writeheader()
            for t in summary.trades:
                writer.writerow(t.to_dict())
        print(f"Trades CSV: {events_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
