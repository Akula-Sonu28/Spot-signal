#!/usr/bin/env python3
"""Sweep exit parameters on historical CSVs — faster than manual Pine A–K testing."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date
from itertools import product
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import format_report, run_backtest
from bot.config import StrategyConfig
from bot.historical_data import build_backtest_dataframe, trading_days_back
from bot.replay import load_candles_csv, run_replay_fast

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


def _load_days(days: int, from_date: str | None, to_date: str | None) -> tuple[pd.DataFrame, list[str]]:
    if from_date:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date) if to_date else date.today()
        day_list: list[date] = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                day_list.append(d)
            d = d.fromordinal(d.toordinal() + 1)
    else:
        day_list = trading_days_back(date.today(), days)

    frames: list[pd.DataFrame] = []
    sessions: list[str] = []
    for d in day_list:
        path = HIST / f"{d.isoformat()}_5m.csv"
        if path.exists():
            frames.append(load_candles_csv(path))
            sessions.append(d.isoformat())
    if not frames:
        raise SystemExit(f"No CSV files found under {HIST}")
    return pd.concat(frames, ignore_index=True), sessions


def _run_one(df: pd.DataFrame, sessions: list[str], cfg: StrategyConfig) -> dict:
    logger = run_replay_fast(df, cfg)
    summary = run_backtest(logger, session_dates=sessions)
    sl_exits = sum(1 for t in summary.trades if t.exit_reason.startswith("STOP"))
    tgt_exits = sum(1 for t in summary.trades if t.exit_reason.startswith("TARGET"))
    eod_exits = sum(1 for t in summary.trades if t.exit_reason == "EOD_SQUARE_OFF")
    return {
        "atr_sl_mult": cfg.atr_sl_mult,
        "rr_ratio": cfg.rr_ratio,
        "sl_mode": cfg.sl_mode,
        "close_only_sl": cfg.close_only_sl,
        "sl_delay_bars": cfg.sl_delay_bars,
        "sl_buffer_pts": cfg.sl_buffer_pts,
        "trades": summary.total_trades,
        "win_rate_pct": round(summary.win_rate_pct, 1),
        "total_pnl_pts": round(summary.total_pnl_pts, 1),
        "profit_factor": round(summary.profit_factor, 2) if summary.profit_factor else None,
        "max_dd_pts": round(summary.max_drawdown_pts, 1),
        "sl_exits": sl_exits,
        "target_exits": tgt_exits,
        "eod_exits": eod_exits,
        "avg_winner": round(summary.avg_winner_pts, 1),
        "avg_loser": round(summary.avg_loser_pts, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search exit params on cached historical CSVs")
    parser.add_argument("--days", type=int, default=60, help="Weekdays back from today")
    parser.add_argument("--from", dest="from_date", help="Start YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="End YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=15, help="Show top N by total P&L")
    parser.add_argument("--out", type=Path, help="Write full JSON results")
    args = parser.parse_args()

    df, sessions = _load_days(args.days, args.from_date, args.to_date)
    print(f"Loaded {len(sessions)} sessions, {len(df)} bars\n")

    base = StrategyConfig()
    atr_mults = [1.2, 1.4, 1.6, 1.8, 2.0, 2.5]
    rr_ratios = [1.5, 1.8, 2.0, 2.5, 3.0]
    sl_modes = ["ATR", "OR_RANGE", "WIDER"]
    close_only = [False, True]
    delays = [0, 1, 2]
    buffers = [0.0, 10.0, 20.0]

    results: list[dict] = []
    total = (
        len(atr_mults)
        * len(rr_ratios)
        * len(sl_modes)
        * len(close_only)
        * len(delays)
        * len(buffers)
    )
    print(f"Sweeping {total} combinations…\n")

    for atr_m, rr, mode, cosl, delay, buf in product(
        atr_mults, rr_ratios, sl_modes, close_only, delays, buffers
    ):
        cfg = replace(
            base,
            atr_sl_mult=atr_m,
            rr_ratio=rr,
            sl_mode=mode,
            close_only_sl=cosl,
            sl_delay_bars=delay,
            sl_buffer_pts=buf,
        )
        results.append(_run_one(df, sessions, cfg))

    ranked = sorted(results, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0), reverse=True)
    top = ranked[: args.top]

    header = (
        f"{'#':>3}  {'ATR':>4} {'RR':>4} {'SL':>6} {'Cls':>3} {'Dly':>3} {'Buf':>4}  "
        f"{'Trd':>4} {'WR%':>5} {'P&L':>8} {'PF':>5} {'DD':>6}  {'SL':>3}/{'TG':>3}/{'EOD':>3}"
    )
    print(header)
    print("-" * len(header))
    for i, row in enumerate(top, 1):
        pf = row["profit_factor"] if row["profit_factor"] is not None else 0.0
        print(
            f"{i:3d}  {row['atr_sl_mult']:4.1f} {row['rr_ratio']:4.1f} {row['sl_mode']:>6} "
            f"{'Y' if row['close_only_sl'] else 'N':>3} {row['sl_delay_bars']:3d} {row['sl_buffer_pts']:4.0f}  "
            f"{row['trades']:4d} {row['win_rate_pct']:5.1f} {row['total_pnl_pts']:8.1f} "
            f"{pf:5.2f} {row['max_dd_pts']:6.1f}  "
            f"{row['sl_exits']:3d}/{row['target_exits']:3d}/{row['eod_exits']:3d}"
        )

    best = ranked[0]
    best_cfg = replace(
        base,
        atr_sl_mult=best["atr_sl_mult"],
        rr_ratio=best["rr_ratio"],
        sl_mode=best["sl_mode"],
        close_only_sl=best["close_only_sl"],
        sl_delay_bars=best["sl_delay_bars"],
        sl_buffer_pts=best["sl_buffer_pts"],
    )
    print("\n=== BEST (by total spot P&L pts) ===")
    best_summary = run_backtest(run_replay_fast(df, best_cfg), session_dates=sessions)
    print(format_report(best_summary, title="Best config"))
    print(
        f"Config: atr_sl_mult={best_cfg.atr_sl_mult} rr={best_cfg.rr_ratio} "
        f"sl_mode={best_cfg.sl_mode} close_only={best_cfg.close_only_sl} "
        f"delay={best_cfg.sl_delay_bars} buffer={best_cfg.sl_buffer_pts}"
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"sessions": sessions, "results": ranked}, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        out_path = OUT / f"exit_sweep_{stamp}.json"
        out_path.write_text(json.dumps({"sessions": sessions, "results": ranked}, indent=2), encoding="utf-8")
        print(f"\nWrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
