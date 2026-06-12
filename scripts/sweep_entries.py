#!/usr/bin/env python3
"""Sweep entry filters on historical CSVs (hold exit config fixed)."""

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

from bot.config import StrategyConfig
from scripts.sweep_exits import _load_days, _run_one

OUT = Path(__file__).resolve().parent.parent / "data" / "research"

# Winning exit from exit sweep — hold constant while tuning entries
EXIT_KW = dict(
    sl_mode="OR_RANGE",
    close_only_sl=True,
    sl_buffer_pts=10.0,
    rr_ratio=2.0,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search entry filters (exit config fixed)")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    df, sessions = _load_days(args.days, None, None)
    print(f"Loaded {len(sessions)} sessions — exit fixed: OR_RANGE + close SL + RR2\n")

    base = replace(StrategyConfig(), **EXIT_KW)
    min_or = [20.0, 25.0, 30.0]
    max_or = [100.0, 105.0, 110.0, 115.0, 120.0]
    adx_mins = [12.0, 15.0, 18.0, 20.0]
    vwap_opts = [True, False]
    max_trades = [2, 3]

    results: list[dict] = []
    combos = list(product(min_or, max_or, adx_mins, vwap_opts, max_trades))
    print(f"Sweeping {len(combos)} entry combinations…\n")

    for mn, mx, adx, vwap, mt in combos:
        if mn >= mx:
            continue
        cfg = replace(
            base,
            min_or_range=mn,
            max_or_range=mx,
            adx_min=adx,
            use_vwap_filter=vwap,
            max_trades_per_day=mt,
        )
        row = _run_one(df, sessions, cfg)
        row["min_or_range"] = mn
        row["max_or_range"] = mx
        row["adx_min"] = adx
        row["use_vwap_filter"] = vwap
        row["max_trades_per_day"] = mt
        results.append(row)

    ranked = sorted(results, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0), reverse=True)
    print(f"{'#':>3}  min  max  ADX  VW  MT   Trd   WR%      P&L    PF")
    print("-" * 58)
    for i, r in enumerate(ranked[: args.top], 1):
        vw = "Y" if r["use_vwap_filter"] else "N"
        print(
            f"{i:3}  {r['min_or_range']:3.0f} {r['max_or_range']:3.0f} "
            f"{r['adx_min']:4.0f}  {vw}  {r['max_trades_per_day']}  "
            f"{r['trades']:3} {r['win_rate_pct']:5.1f} {r['total_pnl_pts']:+8.1f} "
            f"{r['profit_factor'] or 0:5.2f}"
        )

    out = OUT / f"entry_sweep_{date.today().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ranked, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
